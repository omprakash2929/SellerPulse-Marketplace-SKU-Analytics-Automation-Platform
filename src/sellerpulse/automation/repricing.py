"""Rule-based repricing engine with strict margin floor guardrails."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.orm import Session

from sellerpulse.analytics.dead_stock import DeadStockService
from sellerpulse.analytics.forecast import StockoutForecastService
from sellerpulse.config import get_settings
from sellerpulse.db.models import SKU, PriceHistory

logger = logging.getLogger(__name__)


@dataclass
class RepricingRecommendation:
    """Actionable price recommendation for a catalog SKU."""

    sku_id: int
    store_id: int
    store_key: str
    sku_code: str
    title: str
    category: str
    cost_price: float
    current_price: float
    suggested_price: float
    price_change: float
    pct_change: float
    current_margin_pct: float
    projected_margin_pct: float
    rule_applied: str  # LIQUIDATION_MARKDOWN, SCARCITY_PREMIUM, MARGIN_FLOOR_RECOVERY
    rationale: str


class RepricingEngine:
    """Evaluates inventory velocity, days of supply, and profitability to generate price adjustments."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.forecast_svc = StockoutForecastService(session)
        self.dead_svc = DeadStockService(session)

    def generate_recommendations(
        self,
        store_id: int | None = None,
        as_of_date: datetime | None = None,
        min_margin_pct: float | None = None,
    ) -> list[RepricingRecommendation]:
        """Evaluate catalog items and generate rule-based repricing recommendations."""
        margin_floor = min_margin_pct if min_margin_pct is not None else self.settings.MARGIN_FLOOR_PCT

        # Fetch forecasts and dead stock evaluations
        forecast_df = self.forecast_svc.get_forecast_df(store_id=store_id, as_of_date=as_of_date)
        dead_df = self.dead_svc.detect_non_performing_stock_df(store_id=store_id, as_of_date=as_of_date)

        dead_sku_map = (
            {row["sku_code"]: row for _, row in dead_df.iterrows()} if not dead_df.empty else {}
        )

        recommendations: list[RepricingRecommendation] = []

        for _, row in forecast_df.iterrows():
            sku_code = str(row["sku_code"])
            cost = float(row["cost_price"])
            curr_price = float(row["retail_price"])
            stock = int(row["current_stock"])
            days_left = float(row["days_of_stock_left"])
            vel = float(row["effective_velocity"])

            # Absolute price floor: Cost * (1 + margin_floor)
            absolute_floor_price = round(cost * (1.0 + margin_floor), 2)
            curr_margin = (
                round(((curr_price - cost) / curr_price) * 100.0, 2) if curr_price > 0 else 0.0
            )

            # Check Rule 1: Margin Floor Violation
            if curr_price < absolute_floor_price:
                suggested_price = absolute_floor_price
                price_delta = round(suggested_price - curr_price, 2)
                pct_delta = round((price_delta / curr_price) * 100.0, 1)
                proj_margin = round(((suggested_price - cost) / suggested_price) * 100.0, 2)

                recommendations.append(
                    RepricingRecommendation(
                        sku_id=int(row["sku_id"]),
                        store_id=int(row["store_id"]),
                        store_key=str(row["store_key"]),
                        sku_code=sku_code,
                        title=str(row["title"]),
                        category=str(row["category"]),
                        cost_price=cost,
                        current_price=curr_price,
                        suggested_price=suggested_price,
                        price_change=price_delta,
                        pct_change=pct_delta,
                        current_margin_pct=curr_margin,
                        projected_margin_pct=proj_margin,
                        rule_applied="MARGIN_FLOOR_RECOVERY",
                        rationale=f"Current price yields {curr_margin}% margin, violating target floor ({margin_floor * 100:.0f}%).",
                    )
                )
                continue

            # Check Rule 2: Dead or Slow Stock Markdown (Liquidation)
            if sku_code in dead_sku_map:
                dead_info = dead_sku_map[sku_code]
                discount_rate = 0.20 if dead_info["classification"] == "DEAD_STOCK" else 0.10
                raw_discounted = round(curr_price * (1.0 - discount_rate), 2)

                # Guardrail: never discount below margin floor
                suggested_price = max(absolute_floor_price, raw_discounted)

                if suggested_price < curr_price:
                    price_delta = round(suggested_price - curr_price, 2)
                    pct_delta = round((price_delta / curr_price) * 100.0, 1)
                    proj_margin = round(((suggested_price - cost) / suggested_price) * 100.0, 2)

                    recommendations.append(
                        RepricingRecommendation(
                            sku_id=int(row["sku_id"]),
                            store_id=int(row["store_id"]),
                            store_key=str(row["store_key"]),
                            sku_code=sku_code,
                            title=str(row["title"]),
                            category=str(row["category"]),
                            cost_price=cost,
                            current_price=curr_price,
                            suggested_price=suggested_price,
                            price_change=price_delta,
                            pct_change=pct_delta,
                            current_margin_pct=curr_margin,
                            projected_margin_pct=proj_margin,
                            rule_applied="LIQUIDATION_MARKDOWN",
                            rationale=(
                                f"Zero sales in {dead_info['days_since_last_sale']} days with {stock} units in stock. "
                                f"Apply {discount_rate * 100:.0f}% liquidation markdown (respecting {margin_floor * 100:.0f}% margin floor)."
                            ),
                        )
                    )
                    continue

            # Check Rule 3: High-Velocity Scarcity Bump (Runway Protection)
            # High velocity (>2 units/day) with low runway (<12 days)
            if vel >= 2.0 and days_left <= 12.0 and stock > 0:
                bump_rate = 0.08  # 8% price increase
                suggested_price = round(curr_price * (1.0 + bump_rate), 2)
                price_delta = round(suggested_price - curr_price, 2)
                pct_delta = round((price_delta / curr_price) * 100.0, 1)
                proj_margin = round(((suggested_price - cost) / suggested_price) * 100.0, 2)

                recommendations.append(
                    RepricingRecommendation(
                        sku_id=int(row["sku_id"]),
                        store_id=int(row["store_id"]),
                        store_key=str(row["store_key"]),
                        sku_code=sku_code,
                        title=str(row["title"]),
                        category=str(row["category"]),
                        cost_price=cost,
                        current_price=curr_price,
                        suggested_price=suggested_price,
                        price_change=price_delta,
                        pct_change=pct_delta,
                        current_margin_pct=curr_margin,
                        projected_margin_pct=proj_margin,
                        rule_applied="SCARCITY_PREMIUM",
                        rationale=(
                            f"High run rate ({vel:.1f} units/day) with only {days_left:.1f} days stock left. "
                            f"Increase price by +{pct_delta:.1f}% to slow depletion rate and capture premium profit."
                        ),
                    )
                )

        return recommendations

    def apply_recommendation(self, rec: RepricingRecommendation) -> bool:
        """Apply repricing recommendation directly to database SKU record and log history."""
        sku = self.session.get(SKU, rec.sku_id)
        if not sku:
            return False

        old_price = sku.retail_price
        sku.retail_price = rec.suggested_price

        # Record audit log
        history = PriceHistory(
            sku_id=sku.id,
            old_price=old_price,
            new_price=rec.suggested_price,
            reason=f"RepricingEngine: {rec.rule_applied} ({rec.rationale})",
            changed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self.session.add(history)
        self.session.flush()
        logger.info(
            "Applied repricing for SKU %s: $%.2f -> $%.2f (%s)",
            rec.sku_code,
            old_price,
            rec.suggested_price,
            rec.rule_applied,
        )
        return True

    def recommendations_to_df(
        self, recommendations: list[RepricingRecommendation]
    ) -> pd.DataFrame:
        """Convert list of recommendations into a pandas DataFrame."""
        if not recommendations:
            return pd.DataFrame()
        return pd.DataFrame([r.__dict__ for r in recommendations])
