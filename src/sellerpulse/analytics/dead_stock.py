"""Dead stock and slow-moving inventory detection engine."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from sellerpulse.config import get_settings
from sellerpulse.db.models import SKU, Order, OrderItem, Store

logger = logging.getLogger(__name__)


@dataclass
class DeadStockSummary:
    """Consolidated financial summary of non-performing inventory."""

    total_dead_skus: int
    total_slow_skus: int
    dead_capital_tied_up: float
    slow_capital_tied_up: float
    total_non_performing_capital: float
    capital_share_pct: float


class DeadStockService:
    """Identifies inventory tying up working capital with zero or sluggish turnover."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def detect_non_performing_stock_df(
        self,
        store_id: int | None = None,
        as_of_date: datetime | None = None,
        dead_days_threshold: int | None = None,
        slow_days_threshold: int | None = None,
    ) -> pd.DataFrame:
        """Detect dead and slow-moving SKUs with tied-up capital and recommended actions."""
        as_of = as_of_date or datetime.now(UTC)
        if as_of.tzinfo is not None:
            as_of_naive = as_of.astimezone(UTC).replace(tzinfo=None)
        else:
            as_of_naive = as_of

        dead_thresh = dead_days_threshold or self.settings.DEAD_STOCK_DAYS
        slow_thresh = slow_days_threshold or self.settings.SLOW_STOCK_DAYS

        # 1. Fetch SKUs with positive stock
        sku_stmt = (
            select(
                SKU.id.label("sku_id"),
                SKU.store_id,
                Store.store_key,
                SKU.sku_code,
                SKU.title,
                SKU.category,
                SKU.cost_price,
                SKU.retail_price,
                SKU.current_stock,
                SKU.reorder_point,
                SKU.created_at if hasattr(SKU, "created_at") else SKU.updated_at.label("created_at"),
            )
            .join(Store, SKU.store_id == Store.id)
            .where(SKU.current_stock > 0)
        )

        if store_id is not None:
            sku_stmt = sku_stmt.where(SKU.store_id == store_id)

        sku_rows = self.session.execute(sku_stmt).all()
        if not sku_rows:
            return pd.DataFrame()

        skus_df = pd.DataFrame(sku_rows, columns=sku_rows[0]._fields)

        # 2. Get last sale date and recent unit volume per SKU
        # Window of interest: up to 365 days prior
        window_start = as_of_naive - timedelta(days=365)
        sales_stmt = (
            select(
                OrderItem.sku_id,
                func.max(Order.order_date).label("last_sale_date"),
                func.sum(OrderItem.quantity).label("units_sold_365d"),
                func.sum(
                    case(
                        (Order.order_date >= as_of_naive - timedelta(days=slow_thresh), OrderItem.quantity),
                        else_=0,
                    )
                ).label("units_sold_slow_window"),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .where(Order.order_date <= as_of_naive, Order.order_date >= window_start)
            .group_by(OrderItem.sku_id)
        )

        sales_rows = self.session.execute(sales_stmt).all()
        sales_df = (
            pd.DataFrame(sales_rows, columns=sales_rows[0]._fields)
            if sales_rows
            else pd.DataFrame(
                columns=["sku_id", "last_sale_date", "units_sold_365d", "units_sold_slow_window"]
            )
        )

        merged = pd.merge(skus_df, sales_df, on="sku_id", how="left")
        merged["units_sold_365d"] = merged["units_sold_365d"].fillna(0).astype(int)
        merged["units_sold_slow_window"] = merged["units_sold_slow_window"].fillna(0).astype(int)

        # Calculate days since last sale
        days_since_last_sale = []
        for _, row in merged.iterrows():
            last_date = row["last_sale_date"]
            if pd.isna(last_date) or last_date is None:
                days_since_last_sale.append(365)
            else:
                days = (as_of_naive - last_date).days
                days_since_last_sale.append(max(0, days))

        merged["days_since_last_sale"] = days_since_last_sale
        merged["capital_tied_up"] = (merged["current_stock"] * merged["cost_price"]).round(2)
        merged["retail_value_tied_up"] = (merged["current_stock"] * merged["retail_price"]).round(2)

        # Classification
        classification = []
        recommendations = []

        for _, row in merged.iterrows():
            dsls = row["days_since_last_sale"]
            units_slow = row["units_sold_slow_window"]
            stock = row["current_stock"]

            if dsls >= dead_thresh:
                classification.append("DEAD_STOCK")
                recommendations.append(
                    f"Dead stock: 0 sales in {dsls} days. Liquidate via clearance sale or bundle with high-velocity items."
                )
            elif dsls >= slow_thresh or (units_slow <= 2 and stock >= 20):
                classification.append("SLOW_MOVING")
                recommendations.append(
                    "Slow-moving inventory: Apply progressive markdown (10-20% discount) to recover capital."
                )
            else:
                classification.append("ACTIVE_HEALTHY")
                recommendations.append("Healthy turnover: maintain standard replenishment.")

        merged["classification"] = classification
        merged["recommendation"] = recommendations

        # Filter only non-performing items (DEAD_STOCK or SLOW_MOVING)
        non_performing = merged[merged["classification"].isin(["DEAD_STOCK", "SLOW_MOVING"])].copy()
        non_performing = non_performing.sort_values(
            by=["capital_tied_up", "days_since_last_sale"], ascending=[False, False]
        ).reset_index(drop=True)

        return non_performing

    def get_summary(
        self,
        store_id: int | None = None,
        as_of_date: datetime | None = None,
    ) -> DeadStockSummary:
        """Compute aggregate working capital tied up in dead and slow-moving stock."""
        df = self.detect_non_performing_stock_df(store_id=store_id, as_of_date=as_of_date)

        # Get total catalog inventory value for share computation
        total_inv_stmt = select(func.sum(SKU.current_stock * SKU.cost_price)).where(
            SKU.current_stock > 0
        )
        if store_id is not None:
            total_inv_stmt = total_inv_stmt.where(SKU.store_id == store_id)
        total_inv_val = float(self.session.execute(total_inv_stmt).scalar() or 0.0)

        if df.empty:
            return DeadStockSummary(
                total_dead_skus=0,
                total_slow_skus=0,
                dead_capital_tied_up=0.0,
                slow_capital_tied_up=0.0,
                total_non_performing_capital=0.0,
                capital_share_pct=0.0,
            )

        dead_df = df[df["classification"] == "DEAD_STOCK"]
        slow_df = df[df["classification"] == "SLOW_MOVING"]

        dead_cap = float(dead_df["capital_tied_up"].sum())
        slow_cap = float(slow_df["capital_tied_up"].sum())
        total_np_cap = dead_cap + slow_cap

        share = round((total_np_cap / total_inv_val) * 100.0, 2) if total_inv_val > 0 else 0.0

        return DeadStockSummary(
            total_dead_skus=len(dead_df),
            total_slow_skus=len(slow_df),
            dead_capital_tied_up=round(dead_cap, 2),
            slow_capital_tied_up=round(slow_cap, 2),
            total_non_performing_capital=round(total_np_cap, 2),
            capital_share_pct=share,
        )
