"""SQLAlchemy 2.0 database models for SellerPulse."""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Return current naive UTC datetime for SQLite storage."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy entities."""

    pass


class Store(Base):
    """Represents a marketplace store/seller account (e.g. Shopify, Amazon US)."""

    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), default="shopify", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    # Relationships
    skus: Mapped[list["SKU"]] = relationship(
        "SKU", back_populates="store", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(
        "Order", back_populates="store", cascade="all, delete-orphan"
    )
    alerts: Mapped[list["AlertLog"]] = relationship(
        "AlertLog", back_populates="store", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Store id={self.id} key='{self.store_key}' name='{self.name}'>"


class SKU(Base):
    """Represents a product catalog item managed within a store."""

    __tablename__ = "skus"
    __table_args__ = (
        UniqueConstraint("store_id", "sku_code", name="uq_store_sku_code"),
        Index("ix_skus_store_category", "store_id", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    cost_price: Mapped[float] = mapped_column(Float, nullable=False)
    retail_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reorder_point: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="ACTIVE", nullable=False
    )  # ACTIVE, DISCONTINUED, LIQUIDATING
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, nullable=False
    )

    # Relationships
    store: Mapped["Store"] = relationship("Store", back_populates="skus")
    order_items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="sku")
    price_history: Mapped[list["PriceHistory"]] = relationship(
        "PriceHistory", back_populates="sku", cascade="all, delete-orphan"
    )
    alerts: Mapped[list["AlertLog"]] = relationship("AlertLog", back_populates="sku")

    def __repr__(self) -> str:
        return f"<SKU id={self.id} code='{self.sku_code}' stock={self.current_stock}>"


class Order(Base):
    """Represents a finalized customer marketplace transaction."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("store_id", "order_number", name="uq_store_order_number"),
        Index("ix_orders_store_date", "store_id", "order_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_number: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    order_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="COMPLETED", nullable=False
    )  # COMPLETED, REFUNDED, CANCELLED
    channel: Mapped[str] = mapped_column(String(64), default="web", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    # Relationships
    store: Mapped["Store"] = relationship("Store", back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Order id={self.id} num='{self.order_number}' date={self.order_date.date()}>"


class OrderItem(Base):
    """Individual line item associated with an order."""

    __tablename__ = "order_items"
    __table_args__ = (Index("ix_order_items_sku_order", "sku_id", "order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False)
    total_price: Mapped[float] = mapped_column(Float, nullable=False)
    is_returned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    return_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="items")
    sku: Mapped["SKU"] = relationship("SKU", back_populates="order_items")

    def __repr__(self) -> str:
        return f"<OrderItem id={self.id} sku_id={self.sku_id} qty={self.quantity}>"


class PriceHistory(Base):
    """Historical audit log of SKU pricing adjustments."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_price: Mapped[float] = mapped_column(Float, nullable=False)
    new_price: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    # Relationships
    sku: Mapped["SKU"] = relationship("SKU", back_populates="price_history")

    def __repr__(self) -> str:
        return f"<PriceHistory sku_id={self.sku_id} {self.old_price}->{self.new_price}>"


class AlertLog(Base):
    """Operational records of automated alerts triggered by background monitors."""

    __tablename__ = "alert_logs"
    __table_args__ = (Index("ix_alerts_store_triggered", "store_id", "triggered_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku_id: Mapped[int | None] = mapped_column(
        ForeignKey("skus.id", ondelete="SET NULL"), nullable=True, index=True
    )
    alert_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # LOW_STOCK, SALES_DROP, DEAD_STOCK
    severity: Mapped[str] = mapped_column(
        String(32), default="WARNING", nullable=False
    )  # INFO, WARNING, CRITICAL
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, nullable=False, index=True
    )

    # Relationships
    store: Mapped["Store"] = relationship("Store", back_populates="alerts")
    sku: Mapped["SKU | None"] = relationship("SKU", back_populates="alerts")

    def __repr__(self) -> str:
        return f"<AlertLog id={self.id} type='{self.alert_type}' severity='{self.severity}'>"
