"""Unit tests for automation modules: alerts, repricing engine, and reports."""

from datetime import UTC, datetime

from sellerpulse.automation.alerts import AlertDispatcher, AlertManager, AlertNotification
from sellerpulse.automation.reports import generate_reports
from sellerpulse.automation.repricing import RepricingEngine
from sellerpulse.db.models import SKU, AlertLog, PriceHistory


def test_alert_dispatcher_and_manager(populated_session):
    mgr = AlertManager(populated_session)
    as_of = datetime(2026, 10, 1, tzinfo=UTC)

    alerts = mgr.run_all_checks(as_of_date=as_of)
    assert isinstance(alerts, list)
    assert len(alerts) > 0

    types = {a.alert_type for a in alerts}
    assert "LOW_STOCK" in types or "SALES_DROP" in types

    # Verify alerts were persisted in the database
    db_alerts = populated_session.query(AlertLog).all()
    assert len(db_alerts) >= len(alerts)


def test_alert_dispatcher_manual(db_session):
    dispatcher = AlertDispatcher()
    notification = AlertNotification(
        store_id=1,
        sku_id=None,
        sku_code="TEST-SKU",
        alert_type="LOW_STOCK",
        severity="CRITICAL",
        message="Manual test alert message",
        metrics={"test_metric": 42},
    )
    log_entry = dispatcher.dispatch(notification, db_session)
    assert log_entry.id is not None
    assert log_entry.severity == "CRITICAL"
    assert "Manual test alert" in log_entry.message


def test_repricing_engine_rules_and_margin_floor(populated_session):
    engine = RepricingEngine(populated_session)
    as_of = datetime(2026, 10, 1, tzinfo=UTC)

    recs = engine.generate_recommendations(as_of_date=as_of, min_margin_pct=0.15)
    assert isinstance(recs, list)

    for rec in recs:
        # Strict margin floor guardrail test: Suggested price must be >= cost * 1.15
        min_allowed_price = round(rec.cost_price * 1.15, 2)
        assert rec.suggested_price >= min_allowed_price
        assert rec.rule_applied in [
            "LIQUIDATION_MARKDOWN",
            "SCARCITY_PREMIUM",
            "MARGIN_FLOOR_RECOVERY",
        ]
        assert rec.rationale != ""

    if recs:
        target_rec = recs[0]
        # Test applying recommendation
        applied = engine.apply_recommendation(target_rec)
        assert applied is True

        sku = populated_session.get(SKU, target_rec.sku_id)
        assert sku.retail_price == target_rec.suggested_price

        # Check PriceHistory entry
        history = (
            populated_session.query(PriceHistory)
            .filter_by(sku_id=target_rec.sku_id)
            .order_by(PriceHistory.id.desc())
            .first()
        )
        assert history is not None
        assert history.new_price == target_rec.suggested_price


def test_report_generation(populated_session, tmp_path):
    reports = generate_reports(
        populated_session,
        report_format="both",
        output_dir=tmp_path / "reports",
    )

    assert "excel" in reports
    assert "pdf" in reports
    assert reports["excel"].exists()
    assert reports["pdf"].exists()
    assert reports["excel"].stat().st_size > 0
    assert reports["pdf"].stat().st_size > 0
