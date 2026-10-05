"""Inventory stockout forecasting and velocity projection engine."""

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from sellerpulse.config import get_settings
from sellerpulse.db.models import SKU, Order, OrderItem, Store

logger = logging.getLogger(__name__)


@dataclass
class StockoutRiskItem:
    """Predictive stockout metric for a specific catalog SKU."""

    sku_id: int
    store_id: int
    store_key: str
    sku_code: str
    title: str
    category: str
    current_stock: int
    lead_time_days: int
    reorder_point: int
    velocity_7d: float
    velocity_30d: float
    effective_velocity: float
    days_of_stock_left: float
    estimated_stockout_date: datetime | None
    risk_level: str  # CRITICAL, WARNING, HEALTHY, OVERSTOCKED, OUT_OF_STOCK
    suggested_reorder_qty: int


class StockoutForecastService:
    """Forecasts depletion rates, stockout dates, and replenishment recommendations."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def get_forecast_df(
        self,
        store_id: int | None = None,
        as_of_date: datetime | None = None,
        target_buffer_days: int = 45,
    ) -> pd.DataFrame:
        """Compute velocity-based run rates and stockout timelines as a pandas DataFrame."""
        as_of = as_of_date or datetime.now(UTC)
        if as_of.tzinfo is not None:
            as_of_naive = as_of.astimezone(UTC).replace(tzinfo=None)
        else:
            as_of_naive = as_of

        # 1. Fetch SKUs
        sku_stmt = select(
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
            SKU.lead_time_days,
        ).join(Store, SKU.store_id == Store.id)

        if store_id is not None:
            sku_stmt = sku_stmt.where(SKU.store_id == store_id)

        sku_rows = self.session.execute(sku_stmt).all()
        if not sku_rows:
            return pd.DataFrame()

        skus_df = pd.DataFrame(sku_rows, columns=sku_rows[0]._fields)

        # 2. Compute 7-day and 30-day unit volumes
        d7_start = as_of_naive - timedelta(days=7)
        d30_start = as_of_naive - timedelta(days=30)

        sales_stmt = (
            select(
                OrderItem.sku_id,
                func.sum(
                    case(
                        (
                            (Order.order_date >= d7_start) & (Order.order_date <= as_of_naive) & (~OrderItem.is_returned),
                            OrderItem.quantity,
                        ),
                        else_=0,
                    )
                ).label("units_7d"),
                func.sum(
                    case(
                        (
                            (Order.order_date >= d30_start) & (Order.order_date <= as_of_naive) & (~OrderItem.is_returned),
                            OrderItem.quantity,
                        ),
                        else_=0,
                    )
                ).label("units_30d"),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .where(Order.order_date <= as_of_naive, Order.order_date >= d30_start)
            .group_by(OrderItem.sku_id)
        )

        sales_rows = self.session.execute(sales_stmt).all()
        sales_df = (
            pd.DataFrame(sales_rows, columns=sales_rows[0]._fields)
            if sales_rows
            else pd.DataFrame(columns=["sku_id", "units_7d", "units_30d"])
        )

        merged = pd.merge(skus_df, sales_df, on="sku_id", how="left")
        merged["units_7d"] = merged["units_7d"].fillna(0).astype(int)
        merged["units_30d"] = merged["units_30d"].fillna(0).astype(int)

        # Velocities: daily unit sales run rates
        merged["velocity_7d"] = (merged["units_7d"] / 7.0).round(2)
        merged["velocity_30d"] = (merged["units_30d"] / 30.0).round(2)

        # Exponentially smoothed run rate: 60% recent 7-day weight + 40% 30-day baseline
        merged["effective_velocity"] = (
            0.60 * merged["velocity_7d"] + 0.40 * merged["velocity_30d"]
        ).round(2)

        days_left_list = []
        stockout_date_list = []
        risk_list = []
        reorder_qty_list = []

        for _, row in merged.iterrows():
            stock = int(row["current_stock"])
            lead = int(row["lead_time_days"])
            reorder_pt = int(row["reorder_point"])
            vel = float(row["effective_velocity"])

            if stock <= 0:
                days_left = 0.0
                stk_dt = as_of_naive
                risk = "OUT_OF_STOCK"
                sug_qty = int(math.ceil(target_buffer_days * (vel if vel > 0 else 1.0)))
            elif vel <= 0.001:
                days_left = 999.0
                stk_dt = None
                risk = "HEALTHY" if stock <= 50 else "OVERSTOCKED"
                sug_qty = 0
            else:
                days_left = round(stock / vel, 1)
                stk_dt = as_of_naive + timedelta(days=int(min(days_left, 3650)))

                # Risk determination
                if days_left <= lead or days_left <= 7.0:
                    risk = "CRITICAL"
                elif days_left <= 14.0 or stock <= reorder_pt:
                    risk = "WARNING"
                elif days_left > 75.0:
                    risk = "OVERSTOCKED"
                else:
                    risk = "HEALTHY"

                target_stock = int(math.ceil(target_buffer_days * vel))
                sug_qty = max(0, target_stock - stock)

            days_left_list.append(days_left)
            stockout_date_list.append(stk_dt)
            risk_list.append(risk)
            reorder_qty_list.append(sug_qty)

        merged["days_of_stock_left"] = days_left_list
        merged["estimated_stockout_date"] = stockout_date_list
        merged["risk_level"] = risk_list
        merged["suggested_reorder_qty"] = reorder_qty_list

        # Sort: CRITICAL, then WARNING, ascending by days_of_stock_left
        risk_priority = {"CRITICAL": 0, "OUT_OF_STOCK": 1, "WARNING": 2, "HEALTHY": 3, "OVERSTOCKED": 4}
        merged["risk_rank"] = merged["risk_level"].map(risk_priority)
        merged = merged.sort_values(by=["risk_rank", "days_of_stock_left"]).drop(columns=["risk_rank"]).reset_index(drop=True)

        return merged

    def get_critical_stockouts(
        self,
        store_id: int | None = None,
        as_of_date: datetime | None = None,
        days_threshold: float = 14.0,
    ) -> list[StockoutRiskItem]:
        """Return list of items requiring immediate inventory replenishment."""
        df = self.get_forecast_df(store_id=store_id, as_of_date=as_of_date)
        if df.empty:
            return []

        critical = df[
            (df["risk_level"].isin(["CRITICAL", "OUT_OF_STOCK", "WARNING"]))
            | (df["days_of_stock_left"] <= days_threshold)
        ]

        items: list[StockoutRiskItem] = []
        for _, row in critical.iterrows():
            items.append(
                StockoutRiskItem(
                    sku_id=int(row["sku_id"]),
                    store_id=int(row["store_id"]),
                    store_key=str(row["store_key"]),
                    sku_code=str(row["sku_code"]),
                    title=str(row["title"]),
                    category=str(row["category"]),
                    current_stock=int(row["current_stock"]),
                    lead_time_days=int(row["lead_time_days"]),
                    reorder_point=int(row["reorder_point"]),
                    velocity_7d=float(row["velocity_7d"]),
                    velocity_30d=float(row["velocity_30d"]),
                    effective_velocity=float(row["effective_velocity"]),
                    days_of_stock_left=float(row["days_of_stock_left"]),
                    estimated_stockout_date=row["estimated_stockout_date"],
                    risk_level=str(row["risk_level"]),
                    suggested_reorder_qty=int(row["suggested_reorder_qty"]),
                )
            )
        return items
