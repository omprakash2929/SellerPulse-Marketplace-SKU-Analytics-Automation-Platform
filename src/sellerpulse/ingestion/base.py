"""Abstract connector interfaces and transfer schemas for marketplace ingestion."""

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field


class RawStore(BaseModel):
    """Normalized store payload from an external marketplace."""

    store_key: str
    name: str
    platform: str = "shopify"
    currency: str = "USD"


class RawSKU(BaseModel):
    """Normalized SKU/product payload from an external catalog."""

    sku_code: str
    title: str
    category: str
    cost_price: float = Field(ge=0.0)
    retail_price: float = Field(ge=0.0)
    current_stock: int = Field(ge=0)
    reorder_point: int = Field(default=15, ge=0)
    lead_time_days: int = Field(default=7, ge=1)
    status: str = "ACTIVE"


class RawOrderItem(BaseModel):
    """Line item within an order payload."""

    sku_code: str
    quantity: int = Field(ge=1)
    unit_price: float = Field(ge=0.0)
    unit_cost: float = Field(ge=0.0)
    total_price: float = Field(ge=0.0)
    is_returned: bool = False
    return_date: datetime | None = None


class RawOrder(BaseModel):
    """Normalized order transaction payload."""

    order_number: str
    order_date: datetime
    customer_email: str | None = None
    total_amount: float = Field(ge=0.0)
    status: str = "COMPLETED"
    channel: str = "web"
    items: list[RawOrderItem] = Field(default_factory=list)


class BaseMarketplaceConnector(ABC):
    """Abstract interface defining required methods for any e-commerce marketplace integration."""

    @abstractmethod
    def test_connection(self) -> bool:
        """Validate credentials and API connectivity."""
        pass

    @abstractmethod
    def fetch_stores(self) -> list[RawStore]:
        """Fetch all connected stores / channels."""
        pass

    @abstractmethod
    def fetch_catalog(self, store_key: str) -> list[RawSKU]:
        """Fetch SKU inventory and pricing for a specific store."""
        pass

    @abstractmethod
    def fetch_orders(self, store_key: str, since: datetime | None = None) -> list[RawOrder]:
        """Fetch historical or incremental orders for a store."""
        pass

    @abstractmethod
    def update_inventory(self, store_key: str, sku_code: str, new_quantity: int) -> bool:
        """Sync inventory level back to the marketplace."""
        pass

    @abstractmethod
    def update_price(self, store_key: str, sku_code: str, new_price: float) -> bool:
        """Sync updated selling price back to the marketplace."""
        pass
