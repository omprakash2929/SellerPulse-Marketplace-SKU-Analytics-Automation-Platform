"""High-fidelity synthetic marketplace data generator for SellerPulse.

Generates 3 stores, 500 SKUs, and 50k+ orders over 12 months with realistic
seasonality, price variations, returns, and deliberate inventory anomalies:
- Dead stock (zero sales in >90 days with capital tied up)
- Slow-moving stock (high days on hand, sluggish velocity)
- Sudden sales drop (top performing SKUs whose sales collapsed recently)
- Imminent stockout risk (high velocity with minimal days of inventory)
"""

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from sellerpulse.ingestion.base import (
    BaseMarketplaceConnector,
    RawOrder,
    RawOrderItem,
    RawSKU,
    RawStore,
)

STORE_DEFINITIONS = [
    {
        "store_key": "store_alpha",
        "name": "Apex Apparel & Gear",
        "platform": "shopify",
        "currency": "USD",
        "categories": ["Apparel", "Footwear", "Activewear", "Accessories"],
    },
    {
        "store_key": "store_beta",
        "name": "Lumina Home & Living",
        "platform": "amazon",
        "currency": "USD",
        "categories": ["Kitchenware", "Bedding & Bath", "Lighting", "Home Decor"],
    },
    {
        "store_key": "store_gamma",
        "name": "Aura Beauty & Wellness",
        "platform": "shopify",
        "currency": "USD",
        "categories": ["Skincare", "Haircare", "Supplements", "Fragrance"],
    },
]

CATEGORY_PRICE_RANGES: dict[str, tuple[float, float, float, float]] = {
    # (min_cost, max_cost, markup_min, markup_max)
    "Apparel": (8.0, 35.0, 1.8, 2.8),
    "Footwear": (18.0, 65.0, 1.7, 2.5),
    "Activewear": (10.0, 40.0, 1.8, 3.0),
    "Accessories": (3.0, 15.0, 2.0, 3.5),
    "Kitchenware": (12.0, 55.0, 1.6, 2.4),
    "Bedding & Bath": (15.0, 70.0, 1.7, 2.6),
    "Lighting": (20.0, 85.0, 1.8, 2.8),
    "Home Decor": (7.0, 38.0, 1.9, 3.0),
    "Skincare": (6.0, 28.0, 2.2, 3.8),
    "Haircare": (5.0, 22.0, 2.0, 3.2),
    "Supplements": (9.0, 30.0, 2.0, 3.5),
    "Fragrance": (14.0, 60.0, 2.2, 4.0),
}


class SyntheticMarketplaceGenerator:
    """Deterministic synthetic data generator modeling realistic marketplace operations."""

    def __init__(
        self,
        seed: int = 42,
        days: int = 365,
        total_skus: int = 500,
        target_orders: int = 52000,
    ) -> None:
        self.seed = seed
        self.days = days
        self.total_skus = total_skus
        self.target_orders = target_orders
        self.rng = random.Random(seed)

        # Anchor base date to a consistent reference
        self.end_date = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
        self.start_date = self.end_date - timedelta(days=days)

        self._stores: list[RawStore] = []
        self._catalog_by_store: dict[str, list[RawSKU]] = {}
        self._sku_metadata: dict[str, dict[str, Any]] = {}
        self._orders_by_store: dict[str, list[RawOrder]] = {}

        self._generate()

    def _generate(self) -> None:
        """Execute full synthetic generation pipeline."""
        self._generate_stores()
        self._generate_skus()
        self._generate_orders()

    def _generate_stores(self) -> None:
        self._stores = [
            RawStore(
                store_key=st["store_key"],
                name=st["name"],
                platform=st["platform"],
                currency=st["currency"],
            )
            for st in STORE_DEFINITIONS
        ]

    def _generate_skus(self) -> None:
        skus_per_store = math.ceil(self.total_skus / len(self._stores))
        sku_counter = 1

        for store_info in STORE_DEFINITIONS:
            s_key = store_info["store_key"]
            self._catalog_by_store[s_key] = []
            categories = store_info["categories"]

            for i in range(skus_per_store):
                if sku_counter > self.total_skus:
                    break

                category = categories[i % len(categories)]
                cost_min, cost_max, markup_min, markup_max = CATEGORY_PRICE_RANGES[category]
                cost_price = round(self.rng.uniform(cost_min, cost_max), 2)
                markup = self.rng.uniform(markup_min, markup_max)
                retail_price = round(cost_price * markup, 2)

                sku_code = f"SKU-{s_key[:3].upper()}-{sku_counter:04d}"
                title = f"{category} Premium Item {sku_counter:03d}"

                # Assign behavioral archetypes:
                # 1. Dead stock: ~4% of SKUs have stock but 0 sales
                # 2. Slow mover: ~6% of SKUs have sluggish sales
                # 3. Sudden drop: ~3% of SKUs (top seller with sudden crash in last 14 days)
                # 4. Stockout risk: ~4% of SKUs (top seller with near zero stock)
                # 5. Normal / Core catalog: rest
                archetype = "normal"
                rand_val = self.rng.random()
                if rand_val < 0.04:
                    archetype = "dead_stock"
                    current_stock = self.rng.randint(60, 250)
                    reorder_point = 15
                elif rand_val < 0.10:
                    archetype = "slow_mover"
                    current_stock = self.rng.randint(40, 180)
                    reorder_point = 15
                elif rand_val < 0.13:
                    archetype = "sudden_drop"
                    current_stock = self.rng.randint(30, 90)
                    reorder_point = 20
                elif rand_val < 0.17:
                    archetype = "stockout_risk"
                    current_stock = self.rng.randint(3, 12)  # Critical low stock!
                    reorder_point = 25
                else:
                    archetype = "normal"
                    # Pareto popularity weight: Pareto power law
                    current_stock = self.rng.randint(20, 300)
                    reorder_point = self.rng.randint(15, 35)

                sku_obj = RawSKU(
                    sku_code=sku_code,
                    title=title,
                    category=category,
                    cost_price=cost_price,
                    retail_price=retail_price,
                    current_stock=current_stock,
                    reorder_point=reorder_point,
                    lead_time_days=self.rng.randint(5, 14),
                    status="ACTIVE",
                )

                self._catalog_by_store[s_key].append(sku_obj)
                self._sku_metadata[sku_code] = {
                    "archetype": archetype,
                    "store_key": s_key,
                    "popularity_weight": self.rng.paretovariate(1.4)
                    if archetype not in ("dead_stock", "slow_mover")
                    else 0.01,
                    "cost_price": cost_price,
                    "retail_price": retail_price,
                }
                sku_counter += 1

    def _generate_orders(self) -> None:
        """Generate transactions across time window applying seasonality and drop triggers."""
        for s_key in self._catalog_by_store:
            self._orders_by_store[s_key] = []

        orders_per_day_base = self.target_orders / self.days
        order_idx = 100000

        curr_date = self.start_date
        while curr_date < self.end_date:
            days_from_end = (self.end_date - curr_date).days
            month = curr_date.month

            # Seasonality multiplier: Q4 boost (Nov/Dec)
            if month in (11, 12):
                season_mult = 1.95 + 0.3 * math.sin(curr_date.day / 5.0)
            elif month in (6, 7):
                season_mult = 1.25  # Summer bump
            else:
                season_mult = 0.90 + 0.15 * math.sin(curr_date.timetuple().tm_yday / 30.0)

            # Weekend effect: Saturday/Sunday +20%
            weekday_mult = 1.20 if curr_date.weekday() in (5, 6) else 1.0

            daily_orders_count = int(
                self.rng.gauss(orders_per_day_base * season_mult * weekday_mult, 15)
            )
            daily_orders_count = max(10, daily_orders_count)

            for _ in range(daily_orders_count):
                order_idx += 1
                store_def = self.rng.choice(STORE_DEFINITIONS)
                s_key = store_def["store_key"]
                catalog = self._catalog_by_store[s_key]

                # Select 1 to 3 items per order
                num_items = self.rng.choices([1, 2, 3], weights=[0.72, 0.22, 0.06])[0]

                # Filter selectable SKUs based on archetype & time
                candidate_skus = []
                weights = []

                for sku in catalog:
                    meta = self._sku_metadata[sku.sku_code]
                    arch = meta["archetype"]

                    # Dead stock: 0 sales in past 100 days
                    if arch == "dead_stock":
                        if days_from_end < 100:
                            continue
                        w = 0.005
                    elif arch == "slow_mover":
                        w = 0.02
                    elif arch == "sudden_drop":
                        # Sudden sales crash in the last 14 days
                        if days_from_end <= 14:
                            continue  # complete collapse
                        w = meta["popularity_weight"] * 1.5
                    else:
                        w = meta["popularity_weight"]

                    candidate_skus.append(sku)
                    weights.append(w)

                if not candidate_skus:
                    continue

                chosen_skus = self.rng.choices(candidate_skus, weights=weights, k=num_items)
                chosen_skus = list({s.sku_code: s for s in chosen_skus}.values())  # deduplicate items in same order

                order_hour = self.rng.randint(6, 23)
                order_minute = self.rng.randint(0, 59)
                order_second = self.rng.randint(0, 59)
                order_datetime = curr_date.replace(
                    hour=order_hour, minute=order_minute, second=order_second
                )

                items: list[RawOrderItem] = []
                total_order_amount = 0.0

                for chosen_sku in chosen_skus:
                    meta = self._sku_metadata[chosen_sku.sku_code]
                    qty = self.rng.choices([1, 2, 3, 4], weights=[0.82, 0.13, 0.04, 0.01])[0]

                    # Minor price variation (e.g. 5% promotion discount randomly)
                    is_promo = self.rng.random() < 0.12
                    unit_price = (
                        round(chosen_sku.retail_price * 0.90, 2)
                        if is_promo
                        else chosen_sku.retail_price
                    )
                    unit_cost = chosen_sku.cost_price
                    line_total = round(unit_price * qty, 2)

                    # Return simulation: 8% overall return probability
                    is_returned = self.rng.random() < 0.08
                    return_dt = None
                    if is_returned:
                        return_days = self.rng.randint(3, 16)
                        return_dt = order_datetime + timedelta(days=return_days)
                        if return_dt > self.end_date:
                            return_dt = None
                            is_returned = False

                    items.append(
                        RawOrderItem(
                            sku_code=chosen_sku.sku_code,
                            quantity=qty,
                            unit_price=unit_price,
                            unit_cost=unit_cost,
                            total_price=line_total,
                            is_returned=is_returned,
                            return_date=return_dt,
                        )
                    )
                    total_order_amount += line_total

                order_status = "COMPLETED"
                if all(item.is_returned for item in items) and items:
                    order_status = "REFUNDED"

                order_obj = RawOrder(
                    order_number=f"ORD-{s_key[:3].upper()}-{order_idx}",
                    order_date=order_datetime,
                    customer_email=f"cust_{self.rng.randint(1000, 99999)}@example.com",
                    total_amount=round(total_order_amount, 2),
                    status=order_status,
                    channel=self.rng.choice(["web", "mobile_app", "marketplace_ad"]),
                    items=items,
                )
                self._orders_by_store[s_key].append(order_obj)

            curr_date += timedelta(days=1)


class SyntheticConnector(BaseMarketplaceConnector):
    """Production-ready synthetic connector conforming to the BaseMarketplaceConnector interface."""

    def __init__(
        self,
        seed: int = 42,
        days: int = 365,
        total_skus: int = 500,
        target_orders: int = 52000,
    ) -> None:
        self.generator = SyntheticMarketplaceGenerator(
            seed=seed, days=days, total_skus=total_skus, target_orders=target_orders
        )

    def test_connection(self) -> bool:
        """Synthetic connector is always available."""
        return True

    def fetch_stores(self) -> list[RawStore]:
        return self.generator._stores

    def fetch_catalog(self, store_key: str) -> list[RawSKU]:
        return self.generator._catalog_by_store.get(store_key, [])

    def fetch_orders(
        self, store_key: str, since: datetime | None = None
    ) -> list[RawOrder]:
        orders = self.generator._orders_by_store.get(store_key, [])
        if since is None:
            return orders
        return [o for o in orders if o.order_date >= since]

    def update_inventory(self, store_key: str, sku_code: str, new_quantity: int) -> bool:
        catalog = self.generator._catalog_by_store.get(store_key, [])
        for item in catalog:
            if item.sku_code == sku_code:
                item.current_stock = new_quantity
                return True
        return False

    def update_price(self, store_key: str, sku_code: str, new_price: float) -> bool:
        catalog = self.generator._catalog_by_store.get(store_key, [])
        for item in catalog:
            if item.sku_code == sku_code:
                item.retail_price = new_price
                return True
        return False
