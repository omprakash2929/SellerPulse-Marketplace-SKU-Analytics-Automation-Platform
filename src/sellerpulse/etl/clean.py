"""Data cleaning, validation, and deduplication logic for marketplace ingestion."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sellerpulse.ingestion.base import RawOrder, RawOrderItem, RawSKU, RawStore

logger = logging.getLogger(__name__)


@dataclass
class CleanResult:
    """Encapsulates cleaned dataset and data hygiene metrics."""

    stores: list[RawStore] = field(default_factory=list)
    skus_by_store: dict[str, list[RawSKU]] = field(default_factory=dict)
    orders_by_store: dict[str, list[RawOrder]] = field(default_factory=dict)
    dropped_skus_count: int = 0
    dropped_orders_count: int = 0
    anomalies_flagged: list[str] = field(default_factory=list)


class DataCleaner:
    """Validates, sanitizes, and deduplicates raw marketplace records."""

    def clean_stores(self, raw_stores: list[RawStore]) -> list[RawStore]:
        """Deduplicate and sanitize store metadata."""
        seen_keys: set[str] = set()
        clean: list[RawStore] = []

        for st in raw_stores:
            key = st.store_key.strip().lower()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            clean.append(
                RawStore(
                    store_key=key,
                    name=st.name.strip(),
                    platform=st.platform.strip().lower(),
                    currency=st.currency.strip().upper(),
                )
            )
        return clean

    def clean_catalog(self, raw_skus: list[RawSKU]) -> tuple[list[RawSKU], int, list[str]]:
        """Validate and deduplicate SKUs."""
        seen_skus: set[str] = set()
        clean: list[RawSKU] = []
        dropped = 0
        anomalies = []

        for sku in raw_skus:
            code = sku.sku_code.strip().upper()
            if not code or code in seen_skus:
                dropped += 1
                continue

            # Validation guardrails
            if sku.cost_price < 0 or sku.retail_price < 0:
                dropped += 1
                anomalies.append(f"Negative price rejected for SKU {code}")
                continue

            if sku.retail_price < sku.cost_price:
                anomalies.append(f"Negative margin on SKU {code}: retail={sku.retail_price} < cost={sku.cost_price}")

            stock = max(0, sku.current_stock)
            reorder = max(0, sku.reorder_point)
            lead = max(1, sku.lead_time_days)

            seen_skus.add(code)
            clean.append(
                RawSKU(
                    sku_code=code,
                    title=sku.title.strip(),
                    category=sku.category.strip() or "General",
                    cost_price=round(sku.cost_price, 2),
                    retail_price=round(sku.retail_price, 2),
                    current_stock=stock,
                    reorder_point=reorder,
                    lead_time_days=lead,
                    status=sku.status.strip().upper(),
                )
            )
        return clean, dropped, anomalies

    def clean_orders(
        self, raw_orders: list[RawOrder], valid_sku_codes: set[str]
    ) -> tuple[list[RawOrder], int, list[str]]:
        """Validate, deduplicate, and reconcile order line items against catalog."""
        seen_orders: set[str] = set()
        clean: list[RawOrder] = []
        dropped = 0
        anomalies = []
        now_utc = datetime.now(UTC)

        for ord_in in raw_orders:
            num = ord_in.order_number.strip().upper()
            if not num or num in seen_orders:
                dropped += 1
                continue

            # Ensure order date is timezone-aware or normalized
            dt = ord_in.order_date
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)

            if dt > now_utc:
                dropped += 1
                anomalies.append(f"Order {num} rejected: order date {dt} is in future.")
                continue

            # Clean and filter line items
            valid_items: list[RawOrderItem] = []
            for item in ord_in.items:
                item_code = item.sku_code.strip().upper()
                if item_code not in valid_sku_codes:
                    anomalies.append(f"Unknown SKU code {item_code} in order {num}")
                    continue

                if item.quantity <= 0 or item.unit_price < 0 or item.unit_cost < 0:
                    continue

                valid_items.append(
                    RawOrderItem(
                        sku_code=item_code,
                        quantity=item.quantity,
                        unit_price=round(item.unit_price, 2),
                        unit_cost=round(item.unit_cost, 2),
                        total_price=round(item.unit_price * item.quantity, 2),
                        is_returned=item.is_returned,
                        return_date=item.return_date,
                    )
                )

            if not valid_items:
                dropped += 1
                continue

            recomputed_total = round(sum(i.total_price for i in valid_items), 2)
            seen_orders.add(num)
            clean.append(
                RawOrder(
                    order_number=num,
                    order_date=dt,
                    customer_email=ord_in.customer_email.strip().lower()
                    if ord_in.customer_email
                    else None,
                    total_amount=recomputed_total,
                    status=ord_in.status.strip().upper(),
                    channel=ord_in.channel.strip().lower() or "web",
                    items=valid_items,
                )
            )
        return clean, dropped, anomalies
