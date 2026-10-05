"""Core SKU and sales performance metrics calculation engine."""

import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from sellerpulse.db.models import SKU, Order, OrderItem, Store

logger = logging.getLogger(__name__)


@dataclass
class SKUMetricItem:
    """Calculated KPI metrics for a single catalog SKU."""

    sku_id: int
    store_id: int
    store_key: str
    sku_code: str
    title: str
    category: str
    cost_price: float
    retail_price: float
    current_stock: int
    reorder_point: int
    lead_time_days: int
    stock_value: float
    units_sold: int
    units_returned: int
    gross_revenue: float
    net_revenue: float
    cogs: float
    gross_profit: float
    margin_pct: float
    return_rate_pct: float
    asp: float
    sell_through_pct: float


class SKUMetricsService:
    """Extracts order line items and computes financial & operational SKU performance."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_sku_metrics_df(
        self,
        store_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> pd.DataFrame:
        """Compute SKU metrics as a typed pandas DataFrame for flexible slicing and charting."""
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
            SKU.status,
        ).join(Store, SKU.store_id == Store.id)

        if store_id is not None:
            sku_stmt = sku_stmt.where(SKU.store_id == store_id)

        sku_rows = self.session.execute(sku_stmt).all()
        if not sku_rows:
            return pd.DataFrame()

        skus_df = pd.DataFrame(sku_rows, columns=sku_rows[0]._fields)

        # 2. Fetch OrderItems joined with Orders
        item_stmt = select(
            OrderItem.sku_id,
            OrderItem.quantity,
            OrderItem.unit_price,
            OrderItem.unit_cost,
            OrderItem.total_price,
            OrderItem.is_returned,
            Order.order_date,
        ).join(Order, OrderItem.order_id == Order.id)

        if store_id is not None:
            item_stmt = item_stmt.where(Order.store_id == store_id)
        if start_date is not None:
            item_stmt = item_stmt.where(Order.order_date >= start_date)
        if end_date is not None:
            item_stmt = item_stmt.where(Order.order_date <= end_date)

        item_rows = self.session.execute(item_stmt).all()
        if not item_rows:
            # All SKUs with 0 sales
            skus_df["units_sold"] = 0
            skus_df["units_returned"] = 0
            skus_df["gross_revenue"] = 0.0
            skus_df["net_revenue"] = 0.0
            skus_df["cogs"] = 0.0
            skus_df["gross_profit"] = 0.0
            skus_df["margin_pct"] = 0.0
            skus_df["return_rate_pct"] = 0.0
            skus_df["asp"] = 0.0
            skus_df["sell_through_pct"] = 0.0
            skus_df["stock_value"] = skus_df["current_stock"] * skus_df["cost_price"]
            return skus_df

        items_df = pd.DataFrame(item_rows, columns=item_rows[0]._fields)

        # 3. Aggregate sales by SKU
        items_df["cogs_line"] = items_df["quantity"] * items_df["unit_cost"]
        items_df["net_revenue_line"] = items_df["total_price"] * (~items_df["is_returned"]).astype(int)
        items_df["returned_qty"] = items_df["quantity"] * items_df["is_returned"].astype(int)

        agg_df = (
            items_df.groupby("sku_id")
            .agg(
                units_sold=("quantity", "sum"),
                units_returned=("returned_qty", "sum"),
                gross_revenue=("total_price", "sum"),
                net_revenue=("net_revenue_line", "sum"),
                cogs=("cogs_line", "sum"),
            )
            .reset_index()
        )

        merged = pd.merge(skus_df, agg_df, on="sku_id", how="left")
        merged["units_sold"] = merged["units_sold"].fillna(0).astype(int)
        merged["units_returned"] = merged["units_returned"].fillna(0).astype(int)
        merged["gross_revenue"] = merged["gross_revenue"].fillna(0.0).round(2)
        merged["net_revenue"] = merged["net_revenue"].fillna(0.0).round(2)
        merged["cogs"] = merged["cogs"].fillna(0.0).round(2)

        # Compute calculated metrics
        merged["gross_profit"] = (merged["net_revenue"] - merged["cogs"]).round(2)
        merged["margin_pct"] = (
            (merged["gross_profit"] / merged["net_revenue"]) * 100.0
        ).where(merged["net_revenue"] > 0, 0.0).round(2)
        merged["return_rate_pct"] = (
            (merged["units_returned"] / merged["units_sold"]) * 100.0
        ).where(merged["units_sold"] > 0, 0.0).round(2)
        merged["asp"] = (
            merged["gross_revenue"] / merged["units_sold"]
        ).where(merged["units_sold"] > 0, 0.0).round(2)
        merged["stock_value"] = (merged["current_stock"] * merged["cost_price"]).round(2)
        merged["sell_through_pct"] = (
            (merged["units_sold"] / (merged["units_sold"] + merged["current_stock"])) * 100.0
        ).where(merged["units_sold"] + merged["current_stock"] > 0, 0.0).round(2)

        return merged

    def get_sku_metrics(
        self,
        store_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[SKUMetricItem]:
        """Compute SKU metrics and return as structured list of DTO objects."""
        df = self.get_sku_metrics_df(store_id=store_id, start_date=start_date, end_date=end_date)
        if df.empty:
            return []

        results: list[SKUMetricItem] = []
        for _, row in df.iterrows():
            results.append(
                SKUMetricItem(
                    sku_id=int(row["sku_id"]),
                    store_id=int(row["store_id"]),
                    store_key=str(row["store_key"]),
                    sku_code=str(row["sku_code"]),
                    title=str(row["title"]),
                    category=str(row["category"]),
                    cost_price=float(row["cost_price"]),
                    retail_price=float(row["retail_price"]),
                    current_stock=int(row["current_stock"]),
                    reorder_point=int(row["reorder_point"]),
                    lead_time_days=int(row["lead_time_days"]),
                    stock_value=float(row["stock_value"]),
                    units_sold=int(row["units_sold"]),
                    units_returned=int(row["units_returned"]),
                    gross_revenue=float(row["gross_revenue"]),
                    net_revenue=float(row["net_revenue"]),
                    cogs=float(row["cogs"]),
                    gross_profit=float(row["gross_profit"]),
                    margin_pct=float(row["margin_pct"]),
                    return_rate_pct=float(row["return_rate_pct"]),
                    asp=float(row["asp"]),
                    sell_through_pct=float(row["sell_through_pct"]),
                )
            )
        return results

    def get_store_summary(
        self,
        store_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, float]:
        """Aggregate executive high-level store KPIs."""
        df = self.get_sku_metrics_df(store_id=store_id, start_date=start_date, end_date=end_date)
        if df.empty:
            return {
                "total_skus": 0,
                "total_units_sold": 0,
                "total_gross_revenue": 0.0,
                "total_net_revenue": 0.0,
                "total_gross_profit": 0.0,
                "overall_margin_pct": 0.0,
                "overall_return_rate_pct": 0.0,
                "total_inventory_value": 0.0,
            }

        total_gross_rev = float(df["gross_revenue"].sum())
        total_net_rev = float(df["net_revenue"].sum())
        total_profit = float(df["gross_profit"].sum())
        total_sold = int(df["units_sold"].sum())
        total_returned = int(df["units_returned"].sum())
        inv_val = float(df["stock_value"].sum())

        return {
            "total_skus": int(len(df)),
            "total_units_sold": total_sold,
            "total_gross_revenue": round(total_gross_rev, 2),
            "total_net_revenue": round(total_net_rev, 2),
            "total_gross_profit": round(total_profit, 2),
            "overall_margin_pct": round((total_profit / total_net_rev) * 100.0, 2)
            if total_net_rev > 0
            else 0.0,
            "overall_return_rate_pct": round((total_returned / total_sold) * 100.0, 2)
            if total_sold > 0
            else 0.0,
            "total_inventory_value": round(inv_val, 2),
        }
