"""Unit tests for marketplace connectors and synthetic data generation."""

from sellerpulse.ingestion.shopify_stub import ShopifyStubConnector
from sellerpulse.ingestion.synthetic import SyntheticConnector


def test_synthetic_connector_fetch_data():
    connector = SyntheticConnector(seed=999, days=30, total_skus=15, target_orders=50)

    assert connector.test_connection() is True

    stores = connector.fetch_stores()
    assert len(stores) == 3
    assert {s.store_key for s in stores} == {"store_alpha", "store_beta", "store_gamma"}

    # Validate catalog size
    total_skus = sum(len(connector.fetch_catalog(s.store_key)) for s in stores)
    assert total_skus == 15

    # Check order generation
    total_orders = sum(len(connector.fetch_orders(s.store_key)) for s in stores)
    assert total_orders > 0

    # Test update inventory and price
    first_store = stores[0].store_key
    first_sku = connector.fetch_catalog(first_store)[0]
    orig_stock = first_sku.current_stock
    assert connector.update_inventory(first_store, first_sku.sku_code, orig_stock + 25) is True
    assert first_sku.current_stock == orig_stock + 25

    assert connector.update_price(first_store, first_sku.sku_code, 199.99) is True
    assert first_sku.retail_price == 199.99


def test_shopify_stub_connector():
    stub = ShopifyStubConnector(shop_domain="test-shop.myshopify.com")
    assert stub.test_connection() is True

    stores = stub.fetch_stores()
    assert len(stores) == 1
    assert stores[0].store_key == "test-shop"

    catalog = stub.fetch_catalog("test-shop")
    assert len(catalog) >= 1
    assert catalog[0].sku_code == "SHOPIFY-DEMO-001"

    assert stub.update_inventory("test-shop", "SHOPIFY-DEMO-001", 100) is True
    assert stub.update_price("test-shop", "SHOPIFY-DEMO-001", 79.99) is True
