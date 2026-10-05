"""ABC Inventory Analysis (80/15/5 Pareto revenue segmentation) engine."""

import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from sellerpulse.analytics.sku_metrics import SKUMetricsService

logger = logging.getLogger(__name__)


@dataclass
class ABCSummary:
    """Summary distribution for an ABC classification run."""

    total_skus: int
    total_revenue: float
    class_a_skus: int
    class_a_sku_pct: float
    class_a_revenue: float
    class_a_revenue_pct: float
    class_b_skus: int
    class_b_sku_pct: float
    class_b_revenue: float
    class_b_revenue_pct: float
    class_c_skus: int
    class_c_sku_pct: float
    class_c_revenue: float
    class_c_revenue_pct: float


class ABCAnalysisService:
    """Performs Pareto 80/15/5 revenue categorization across catalog SKUs."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.metrics_service = SKUMetricsService(session)

    def calculate_abc_df(
        self,
        store_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> pd.DataFrame:
        """Assign ABC classes, cumulative revenue shares, and operational recommendations."""
        df = self.metrics_service.get_sku_metrics_df(
            store_id=store_id, start_date=start_date, end_date=end_date
        )

        if df.empty:
            return pd.DataFrame()

        # Sort descending by gross revenue, tie-break by units sold
        df = df.sort_values(
            by=["gross_revenue", "units_sold"], ascending=[False, False]
        ).reset_index(drop=True)
        total_revenue = float(df["gross_revenue"].sum())

        if total_revenue <= 0.0:
            df["cumulative_revenue"] = 0.0
            df["cumulative_revenue_pct"] = 0.0
            df["revenue_share_pct"] = 0.0
            df["abc_class"] = "C"
            df["recommendation"] = "No revenue recorded: evaluate catalog viability or markdown."
            return df

        df["revenue_share_pct"] = ((df["gross_revenue"] / total_revenue) * 100.0).round(2)
        df["cumulative_revenue"] = df["gross_revenue"].cumsum().round(2)
        df["cumulative_revenue_pct"] = ((df["cumulative_revenue"] / total_revenue) * 100.0).round(2)

        # Classification logic (80 / 15 / 5)
        # Class A: Top tier accumulating up to 80% (inclusive of the item crossing 80%)
        # Class B: Items between 80% and 95%
        # Class C: Tail items 95% to 100%
        abc_classes = []
        recommendations = []

        for _, row in df.iterrows():
            cum_pct = row["cumulative_revenue_pct"]
            prev_cum_pct = cum_pct - row["revenue_share_pct"]

            if prev_cum_pct < 80.0:
                cls = "A"
                rec = "Core Revenue Driver: Maintain buffer stock, daily velocity check, prioritize supplier fulfillment."
            elif prev_cum_pct < 95.0:
                cls = "B"
                rec = "Consistent Performer: Maintain standard safety stock, automated weekly replenishment."
            else:
                cls = "C"
                rec = "Long Tail / Low Revenue: Review reorder quantities; evaluate clearance or liquidation if turnover is slow."

            abc_classes.append(cls)
            recommendations.append(rec)

        df["abc_class"] = abc_classes
        df["recommendation"] = recommendations
        return df

    def get_summary(
        self,
        store_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> ABCSummary:
        """Compute high-level ABC segment distributions."""
        df = self.calculate_abc_df(store_id=store_id, start_date=start_date, end_date=end_date)
        if df.empty:
            return ABCSummary(
                total_skus=0,
                total_revenue=0.0,
                class_a_skus=0,
                class_a_sku_pct=0.0,
                class_a_revenue=0.0,
                class_a_revenue_pct=0.0,
                class_b_skus=0,
                class_b_sku_pct=0.0,
                class_b_revenue=0.0,
                class_b_revenue_pct=0.0,
                class_c_skus=0,
                class_c_sku_pct=0.0,
                class_c_revenue=0.0,
                class_c_revenue_pct=0.0,
            )

        total_skus = len(df)
        total_rev = float(df["gross_revenue"].sum())

        a_df = df[df["abc_class"] == "A"]
        b_df = df[df["abc_class"] == "B"]
        c_df = df[df["abc_class"] == "C"]

        a_rev = float(a_df["gross_revenue"].sum())
        b_rev = float(b_df["gross_revenue"].sum())
        c_rev = float(c_df["gross_revenue"].sum())

        return ABCSummary(
            total_skus=total_skus,
            total_revenue=round(total_rev, 2),
            class_a_skus=len(a_df),
            class_a_sku_pct=round((len(a_df) / total_skus) * 100.0, 1) if total_skus else 0.0,
            class_a_revenue=round(a_rev, 2),
            class_a_revenue_pct=round((a_rev / total_rev) * 100.0, 1) if total_rev else 0.0,
            class_b_skus=len(b_df),
            class_b_sku_pct=round((len(b_df) / total_skus) * 100.0, 1) if total_skus else 0.0,
            class_b_revenue=round(b_rev, 2),
            class_b_revenue_pct=round((b_rev / total_rev) * 100.0, 1) if total_rev else 0.0,
            class_c_skus=len(c_df),
            class_c_sku_pct=round((len(c_df) / total_skus) * 100.0, 1) if total_skus else 0.0,
            class_c_revenue=round(c_rev, 2),
            class_c_revenue_pct=round((c_rev / total_rev) * 100.0, 1) if total_rev else 0.0,
        )
