"""Unit tests for SKU metrics, ABC classification, dead stock detection, and stockout forecasting."""

from datetime import UTC, datetime

from sellerpulse.analytics.abc_analysis import ABCAnalysisService
from sellerpulse.analytics.dead_stock import DeadStockService
from sellerpulse.analytics.forecast import StockoutForecastService
from sellerpulse.analytics.sku_metrics import SKUMetricsService


def test_sku_metrics_calculation(populated_session):
    service = SKUMetricsService(populated_session)
    df = service.get_sku_metrics_df()

    assert not df.empty
    assert "gross_revenue" in df.columns
    assert "margin_pct" in df.columns
    assert "return_rate_pct" in df.columns
    assert "asp" in df.columns
    assert "sell_through_pct" in df.columns

    # Validate math properties
    for _, row in df.iterrows():
        if row["units_sold"] > 0:
            assert row["asp"] > 0.0
            assert 0.0 <= row["return_rate_pct"] <= 100.0
        assert row["stock_value"] >= 0.0

    summary = service.get_store_summary()
    assert summary["total_skus"] == 30
    assert summary["total_gross_revenue"] > 0.0
    assert summary["total_units_sold"] > 0
    assert 0.0 <= summary["overall_return_rate_pct"] <= 100.0


def test_abc_analysis_segmentation(populated_session):
    service = ABCAnalysisService(populated_session)
    df = service.calculate_abc_df()

    assert not df.empty
    assert set(df["abc_class"].unique()).issubset({"A", "B", "C"})
    assert "recommendation" in df.columns

    # Validate Pareto ordering: cumulative revenue percentage is monotonically increasing
    cum_pcts = df["cumulative_revenue_pct"].tolist()
    assert all(x <= y for x, y in zip(cum_pcts, cum_pcts[1:], strict=False))

    summary = service.get_summary()
    assert summary.total_skus == 30
    assert summary.total_revenue > 0.0
    # Class A should account for approximately 80% (typically 75%-85%)
    assert summary.class_a_revenue_pct >= 70.0


def test_abc_analysis_empty_and_zero_revenue(db_session):
    service = ABCAnalysisService(db_session)
    empty_summary = service.get_summary()
    assert empty_summary.total_skus == 0
    assert empty_summary.total_revenue == 0.0


def test_dead_stock_detection(populated_session):
    service = DeadStockService(populated_session)
    as_of = datetime(2026, 10, 1, tzinfo=UTC)

    df = service.detect_non_performing_stock_df(as_of_date=as_of, dead_days_threshold=60)
    summary = service.get_summary(as_of_date=as_of)

    # In our synthetic generator, dead stock archetypes are deliberately injected
    assert summary.total_non_performing_capital >= 0.0
    if not df.empty:
        assert set(df["classification"].unique()).issubset({"DEAD_STOCK", "SLOW_MOVING"})
        assert all(row["capital_tied_up"] > 0 for _, row in df.iterrows())
        assert all("recommendation" in row for _, row in df.iterrows())


def test_stockout_forecast(populated_session):
    service = StockoutForecastService(populated_session)
    as_of = datetime(2026, 10, 1, tzinfo=UTC)

    df = service.get_forecast_df(as_of_date=as_of)
    assert not df.empty
    assert "days_of_stock_left" in df.columns
    assert "effective_velocity" in df.columns
    assert "risk_level" in df.columns

    critical_items = service.get_critical_stockouts(as_of_date=as_of, days_threshold=14.0)
    assert isinstance(critical_items, list)
    for item in critical_items:
        assert item.risk_level in ["CRITICAL", "WARNING", "OUT_OF_STOCK"]


def test_forecast_zero_velocity(db_session):
    # Test behavior when there are no sales
    service = StockoutForecastService(db_session)
    df = service.get_forecast_df()
    assert df.empty
