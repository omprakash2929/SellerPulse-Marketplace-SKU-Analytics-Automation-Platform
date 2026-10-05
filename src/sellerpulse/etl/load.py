"""Idempotent loading pipeline for SellerPulse SQLite storage."""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sellerpulse.db.models import SKU, Order, OrderItem, PriceHistory, Store
from sellerpulse.db.session import get_db_context, init_db
from sellerpulse.etl.clean import DataCleaner
from sellerpulse.ingestion.base import BaseMarketplaceConnector

logger = logging.getLogger(__name__)


@dataclass
class ETLRunSummary:
    """Execution telemetry and record counts from an ETL sync."""

    stores_synced: int = 0
    skus_synced: int = 0
    orders_synced: int = 0
    order_items_synced: int = 0
    price_changes_detected: int = 0
    duration_seconds: float = 0.0
    status: str = "SUCCESS"
    error_message: str | None = None


class ETLLoader:
    """Manages transactional, idempotent loading of cleansed data into SQLite."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def load_stores(self, clean_stores: list) -> dict[str, Store]:
        """Upsert stores by store_key and return dictionary mapping store_key to Store entity."""
        store_map: dict[str, Store] = {}
        for raw_st in clean_stores:
            stmt = select(Store).where(Store.store_key == raw_st.store_key)
            existing = self.session.execute(stmt).scalar_one_or_none()
            if existing:
                existing.name = raw_st.name
                existing.platform = raw_st.platform
                existing.currency = raw_st.currency
                store_map[raw_st.store_key] = existing
            else:
                new_store = Store(
                    store_key=raw_st.store_key,
                    name=raw_st.name,
                    platform=raw_st.platform,
                    currency=raw_st.currency,
                    is_active=True,
                )
                self.session.add(new_store)
                self.session.flush()
                store_map[raw_st.store_key] = new_store
        self.session.flush()
        return store_map

    def load_catalog(
        self, store: Store, clean_skus: list
    ) -> tuple[dict[str, SKU], int]:
        """Upsert SKUs, update inventory, and log PriceHistory if retail price changed."""
        sku_map: dict[str, SKU] = {}
        price_changes = 0

        # Preload existing SKUs for this store
        existing_stmt = select(SKU).where(SKU.store_id == store.id)
        existing_skus = {s.sku_code: s for s in self.session.execute(existing_stmt).scalars().all()}

        for raw_sku in clean_skus:
            if raw_sku.sku_code in existing_skus:
                existing = existing_skus[raw_sku.sku_code]
                if abs(existing.retail_price - raw_sku.retail_price) > 0.001:
                    price_history = PriceHistory(
                        sku_id=existing.id,
                        old_price=existing.retail_price,
                        new_price=raw_sku.retail_price,
                        reason="ETL price sync",
                        changed_at=datetime.now(UTC),
                    )
                    self.session.add(price_history)
                    price_changes += 1

                existing.title = raw_sku.title
                existing.category = raw_sku.category
                existing.cost_price = raw_sku.cost_price
                existing.retail_price = raw_sku.retail_price
                existing.current_stock = raw_sku.current_stock
                existing.reorder_point = raw_sku.reorder_point
                existing.lead_time_days = raw_sku.lead_time_days
                existing.status = raw_sku.status
                sku_map[raw_sku.sku_code] = existing
            else:
                new_sku = SKU(
                    store_id=store.id,
                    sku_code=raw_sku.sku_code,
                    title=raw_sku.title,
                    category=raw_sku.category,
                    cost_price=raw_sku.cost_price,
                    retail_price=raw_sku.retail_price,
                    current_stock=raw_sku.current_stock,
                    reorder_point=raw_sku.reorder_point,
                    lead_time_days=raw_sku.lead_time_days,
                    status=raw_sku.status,
                )
                self.session.add(new_sku)
                self.session.flush()
                sku_map[raw_sku.sku_code] = new_sku

        self.session.flush()
        return sku_map, price_changes

    def load_orders(
        self, store: Store, clean_orders: list, sku_map: dict[str, SKU], batch_size: int = 1000
    ) -> tuple[int, int]:
        """Idempotently load orders and associated line items."""
        # Find existing order numbers for this store to prevent duplicate primary keys
        existing_orders_stmt = select(Order.order_number).where(Order.store_id == store.id)
        existing_numbers = set(self.session.execute(existing_orders_stmt).scalars().all())

        new_orders = [o for o in clean_orders if o.order_number not in existing_numbers]
        orders_inserted = 0
        items_inserted = 0

        # Chunk insertion to keep memory footprint bounded
        for i in range(0, len(new_orders), batch_size):
            chunk = new_orders[i : i + batch_size]
            for raw_ord in chunk:
                order_dt = raw_ord.order_date
                if order_dt.tzinfo is not None:
                    # SQLite stores naive datetimes natively
                    order_dt = order_dt.astimezone(UTC).replace(tzinfo=None)

                order_entity = Order(
                    store_id=store.id,
                    order_number=raw_ord.order_number,
                    order_date=order_dt,
                    customer_email=raw_ord.customer_email,
                    total_amount=raw_ord.total_amount,
                    status=raw_ord.status,
                    channel=raw_ord.channel,
                )
                self.session.add(order_entity)
                self.session.flush()
                orders_inserted += 1

                for item in raw_ord.items:
                    sku_entity = sku_map.get(item.sku_code)
                    if not sku_entity:
                        continue

                    ret_dt = item.return_date
                    if ret_dt is not None and ret_dt.tzinfo is not None:
                        ret_dt = ret_dt.astimezone(UTC).replace(tzinfo=None)

                    item_entity = OrderItem(
                        order_id=order_entity.id,
                        sku_id=sku_entity.id,
                        quantity=item.quantity,
                        unit_price=item.unit_price,
                        unit_cost=item.unit_cost,
                        total_price=item.total_price,
                        is_returned=item.is_returned,
                        return_date=ret_dt,
                    )
                    self.session.add(item_entity)
                    items_inserted += 1

            self.session.flush()

        return orders_inserted, items_inserted


def run_etl_pipeline(
    connector: BaseMarketplaceConnector,
    session: Session | None = None,
) -> ETLRunSummary:
    """Execute complete end-to-end ETL sync: Extract -> Clean/Validate -> Load."""
    start_time = time.perf_counter()
    summary = ETLRunSummary()
    cleaner = DataCleaner()

    def _execute(sess: Session) -> None:
        init_db(sess.bind)
        loader = ETLLoader(sess)

        # 1. Extract & Clean Stores
        raw_stores = connector.fetch_stores()
        clean_stores = cleaner.clean_stores(raw_stores)
        store_entities = loader.load_stores(clean_stores)
        summary.stores_synced = len(store_entities)

        # 2. Extract, Clean & Load Catalogs and Orders per Store
        for raw_st in clean_stores:
            s_key = raw_st.store_key
            store_entity = store_entities[s_key]

            # Catalog
            raw_skus = connector.fetch_catalog(s_key)
            clean_skus, _, _ = cleaner.clean_catalog(raw_skus)
            sku_entities, price_changes = loader.load_catalog(store_entity, clean_skus)
            summary.skus_synced += len(sku_entities)
            summary.price_changes_detected += price_changes

            # Orders
            valid_codes = set(sku_entities.keys())
            raw_orders = connector.fetch_orders(s_key)
            clean_orders, _, _ = cleaner.clean_orders(raw_orders, valid_codes)
            orders_count, items_count = loader.load_orders(
                store_entity, clean_orders, sku_entities
            )
            summary.orders_synced += orders_count
            summary.order_items_synced += items_count

        summary.duration_seconds = round(time.perf_counter() - start_time, 2)
        summary.status = "SUCCESS"

    if session is not None:
        _execute(session)
    else:
        with get_db_context() as sess:
            _execute(sess)

    logger.info(
        "ETL Pipeline completed in %.2fs: %d stores, %d SKUs, %d orders, %d items",
        summary.duration_seconds,
        summary.stores_synced,
        summary.skus_synced,
        summary.orders_synced,
        summary.order_items_synced,
    )
    return summary
