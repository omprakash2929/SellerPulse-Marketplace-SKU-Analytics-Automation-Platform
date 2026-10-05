"""Unit tests for ETL data cleaning, deduplication, and idempotent loading."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from sellerpulse.db.models import Order, PriceHistory
from sellerpulse.etl.clean import DataCleaner
from sellerpulse.etl.load import ETLLoader, run_etl_pipeline
from sellerpulse.ingestion.base import RawOrder, RawOrderItem, RawSKU, RawStore
from sellerpulse.ingestion.synthetic import SyntheticConnector


def test_data_cleaner_store_deduplication():
    cleaner = DataCleaner()
    raw = [
        RawStore(store_key="store_a", name="Store A", platform="shopify", currency="USD"),
        RawStore(store_key="store_a", name="Store A duplicate", platform="shopify", currency="USD"),
        RawStore(store_key="STORE_B ", name="Store B", platform="amazon", currency="usd"),
    ]
    cleaned = cleaner.clean_stores(raw)
    assert len(cleaned) == 2
    assert cleaned[0].store_key == "store_a"
    assert cleaned[1].store_key == "store_b"
    assert cleaned[1].currency == "USD"


def test_data_cleaner_sku_validation():
    cleaner = DataCleaner()
    raw = [
        RawSKU(sku_code="SKU-OK", title="Valid SKU", category="Apparel", cost_price=10.0, retail_price=25.0, current_stock=50),
        RawSKU.model_construct(sku_code="SKU-NEG-PRICE", title="Negative Price", category="Apparel", cost_price=-5.0, retail_price=20.0, current_stock=10, reorder_point=15, lead_time_days=7, status="ACTIVE"),
        RawSKU(sku_code="SKU-DUP", title="First Dup", category="Apparel", cost_price=10.0, retail_price=20.0, current_stock=10),
        RawSKU(sku_code="sku-dup", title="Second Dup", category="Apparel", cost_price=10.0, retail_price=20.0, current_stock=10),
    ]
    cleaned, dropped, anomalies = cleaner.clean_catalog(raw)
    assert len(cleaned) == 2  # SKU-OK and SKU-DUP
    assert dropped == 2
    assert any("Negative price" in a for a in anomalies)


def test_data_cleaner_order_validation():
    cleaner = DataCleaner()
    now = datetime.now(UTC)
    future_date = now + timedelta(days=5)

    valid_sku_codes = {"SKU-1", "SKU-2"}
    raw_orders = [
        RawOrder(
            order_number="ORD-VALID",
            order_date=now - timedelta(days=2),
            customer_email="user@test.com",
            total_amount=50.0,
            items=[
                RawOrderItem(sku_code="SKU-1", quantity=2, unit_price=25.0, unit_cost=10.0, total_price=50.0),
            ],
        ),
        RawOrder(
            order_number="ORD-FUTURE",
            order_date=future_date,
            total_amount=50.0,
            items=[
                RawOrderItem(sku_code="SKU-1", quantity=1, unit_price=50.0, unit_cost=20.0, total_price=50.0),
            ],
        ),
        RawOrder(
            order_number="ORD-UNKNOWN-SKU",
            order_date=now - timedelta(days=1),
            total_amount=30.0,
            items=[
                RawOrderItem(sku_code="UNKNOWN-CODE", quantity=1, unit_price=30.0, unit_cost=10.0, total_price=30.0),
            ],
        ),
    ]
    cleaned, dropped, anomalies = cleaner.clean_orders(raw_orders, valid_sku_codes)
    assert len(cleaned) == 1
    assert cleaned[0].order_number == "ORD-VALID"
    assert dropped == 2


def test_etl_loader_idempotency_and_price_history(db_session):
    loader = ETLLoader(db_session)

    # 1. Load store
    stores = loader.load_stores([
        RawStore(store_key="store_test", name="Test Store", platform="shopify", currency="USD")
    ])
    test_store = stores["store_test"]

    # 2. Load catalog
    skus, price_changes = loader.load_catalog(
        test_store,
        [RawSKU(sku_code="SKU-TEST-01", title="Test SKU", category="Gear", cost_price=20.0, retail_price=50.0, current_stock=100)],
    )
    assert len(skus) == 1
    assert price_changes == 0

    # 3. Reload catalog with modified price
    skus_v2, price_changes_v2 = loader.load_catalog(
        test_store,
        [RawSKU(sku_code="SKU-TEST-01", title="Test SKU", category="Gear", cost_price=20.0, retail_price=59.99, current_stock=95)],
    )
    assert price_changes_v2 == 1
    db_session.flush()

    # Verify price history table
    history_stmt = select(PriceHistory)
    history_records = db_session.execute(history_stmt).scalars().all()
    assert len(history_records) == 1
    assert history_records[0].old_price == 50.0
    assert history_records[0].new_price == 59.99

    # 4. Full pipeline idempotency test
    connector = SyntheticConnector(seed=42, days=15, total_skus=10, target_orders=30)
    summary1 = run_etl_pipeline(connector, session=db_session)
    assert summary1.status == "SUCCESS"
    assert summary1.orders_synced > 0

    initial_orders_count = len(db_session.execute(select(Order)).scalars().all())

    # Run again: should insert 0 new orders
    summary2 = run_etl_pipeline(connector, session=db_session)
    second_orders_count = len(db_session.execute(select(Order)).scalars().all())
    assert summary2.orders_synced == 0
    assert second_orders_count == initial_orders_count
