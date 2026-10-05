"""FastAPI REST service for SellerPulse analytics and automation."""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sellerpulse import __version__
from sellerpulse.analytics.abc_analysis import ABCAnalysisService
from sellerpulse.analytics.dead_stock import DeadStockService
from sellerpulse.analytics.forecast import StockoutForecastService
from sellerpulse.analytics.sku_metrics import SKUMetricsService
from sellerpulse.api.observability import (
    ALERTS_TRIGGERED_TOTAL,
    CONTENT_TYPE_LATEST,
    HealthResponse,
    PrometheusMiddleware,
    generate_latest,
)
from sellerpulse.automation.alerts import AlertManager
from sellerpulse.automation.reports import generate_reports
from sellerpulse.automation.repricing import RepricingEngine
from sellerpulse.config import get_settings
from sellerpulse.db.models import AlertLog, Store
from sellerpulse.db.session import get_db, init_db

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
START_TIME = time.time()

app = FastAPI(
    title="SellerPulse Marketplace Analytics API",
    description="Enterprise REST API for e-commerce SKU analytics, ABC classification, stockout forecasting, and repricing automation.",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(PrometheusMiddleware)


@app.on_event("startup")
def on_startup() -> None:
    """Initialize database tables on service startup."""
    init_db()


def verify_api_key(api_key: str | None = Security(API_KEY_HEADER)) -> str:
    """Authenticate incoming HTTP request against configured API Key."""
    settings = get_settings()
    if not api_key or api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header.",
        )
    return api_key


# ==============================================================================
# Observability & Health Endpoints (Public)
# ==============================================================================


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Observability"],
    summary="Service Health Check",
)
def health_check(session: Session = Depends(get_db)) -> HealthResponse:
    """Perform database ping and report service uptime."""
    settings = get_settings()
    db_status = "healthy"
    try:
        session.execute(text("SELECT 1;"))
    except Exception as exc:
        logger.error("Health check database ping failed: %s", exc)
        db_status = "unhealthy"

    return HealthResponse(
        status="ok" if db_status == "healthy" else "degraded",
        database=db_status,
        uptime_seconds=round(time.time() - START_TIME, 2),
        version=__version__,
        environment=settings.ENVIRONMENT,
    )


@app.get("/metrics", tags=["Observability"], summary="Prometheus Metrics Scrape")
def prometheus_metrics() -> Response:
    """Export Prometheus telemetry metrics."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ==============================================================================
# Store Management Endpoints
# ==============================================================================


@app.get("/api/v1/stores", tags=["Stores"], summary="List Connected Stores")
def list_stores(
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[dict[str, Any]]:
    """Retrieve all marketplace stores configured in SellerPulse."""
    stores = session.execute(select(Store)).scalars().all()
    return [
        {
            "id": s.id,
            "store_key": s.store_key,
            "name": s.name,
            "platform": s.platform,
            "currency": s.currency,
            "is_active": s.is_active,
            "created_at": s.created_at.isoformat(),
        }
        for s in stores
    ]


@app.get("/api/v1/stores/{store_id}/summary", tags=["Stores"], summary="Store Executive KPI Scorecard")
def get_store_kpi_summary(
    store_id: int,
    days: int = Query(default=365, ge=1, le=730),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Retrieve executive KPI scorecard metrics for a specific store."""
    anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
    start_dt = anchor_end - timedelta(days=days)

    metrics_svc = SKUMetricsService(session)
    dead_svc = DeadStockService(session)
    forecast_svc = StockoutForecastService(session)

    summary = metrics_svc.get_store_summary(store_id=store_id, start_date=start_dt, end_date=anchor_end)
    dead_summary = dead_svc.get_summary(store_id=store_id, as_of_date=anchor_end)
    critical_stockouts = forecast_svc.get_critical_stockouts(store_id=store_id, as_of_date=anchor_end)

    return {
        "store_id": store_id,
        "timeframe_days": days,
        "kpis": summary,
        "dead_stock": dead_summary.__dict__,
        "stockout_risk_skus_count": len(critical_stockouts),
    }


# ==============================================================================
# Analytics Endpoints
# ==============================================================================


@app.get("/api/v1/analytics/sku-metrics", tags=["Analytics"], summary="SKU Performance Metrics")
def get_sku_metrics(
    store_id: int | None = Query(default=None),
    days: int = Query(default=90, ge=1, le=730),
    category: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Query granular SKU performance metrics (revenue, margin, ASP, return rate)."""
    anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
    start_dt = anchor_end - timedelta(days=days)

    metrics_svc = SKUMetricsService(session)
    df = metrics_svc.get_sku_metrics_df(store_id=store_id, start_date=start_dt, end_date=anchor_end)

    if df.empty:
        return {"total": 0, "items": []}

    if category:
        df = df[df["category"].str.lower() == category.lower()]

    total = len(df)
    paged = df.iloc[offset : offset + limit]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": paged.to_dict(orient="records"),
    }


@app.get("/api/v1/analytics/abc", tags=["Analytics"], summary="ABC Inventory Segmentation")
def get_abc_analysis(
    store_id: int | None = Query(default=None),
    days: int = Query(default=365, ge=1, le=730),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Calculate Pareto 80/15/5 revenue classification and recommendations."""
    anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
    start_dt = anchor_end - timedelta(days=days)

    abc_svc = ABCAnalysisService(session)
    summary = abc_svc.get_summary(store_id=store_id, start_date=start_dt, end_date=anchor_end)
    df = abc_svc.calculate_abc_df(store_id=store_id, start_date=start_dt, end_date=anchor_end)

    return {
        "summary": summary.__dict__,
        "items": df.to_dict(orient="records") if not df.empty else [],
    }


@app.get("/api/v1/analytics/dead-stock", tags=["Analytics"], summary="Dead Stock & Slow Movers")
def get_dead_stock(
    store_id: int | None = Query(default=None),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Retrieve dead and slow-moving inventory with capital tied up."""
    anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
    dead_svc = DeadStockService(session)
    summary = dead_svc.get_summary(store_id=store_id, as_of_date=anchor_end)
    df = dead_svc.detect_non_performing_stock_df(store_id=store_id, as_of_date=anchor_end)

    return {
        "summary": summary.__dict__,
        "items": df.to_dict(orient="records") if not df.empty else [],
    }


@app.get("/api/v1/analytics/stockout-risk", tags=["Analytics"], summary="Stockout Depletion Forecast")
def get_stockout_risk(
    store_id: int | None = Query(default=None),
    days_threshold: float = Query(default=14.0, ge=1.0, le=90.0),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[dict[str, Any]]:
    """Retrieve catalog items projected to stock out within the specified days threshold."""
    anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
    forecast_svc = StockoutForecastService(session)
    items = forecast_svc.get_critical_stockouts(
        store_id=store_id, as_of_date=anchor_end, days_threshold=days_threshold
    )
    return [
        {
            "sku_id": i.sku_id,
            "store_id": i.store_id,
            "store_key": i.store_key,
            "sku_code": i.sku_code,
            "title": i.title,
            "category": i.category,
            "current_stock": i.current_stock,
            "effective_velocity": i.effective_velocity,
            "days_of_stock_left": i.days_of_stock_left,
            "estimated_stockout_date": i.estimated_stockout_date.isoformat()
            if i.estimated_stockout_date
            else None,
            "risk_level": i.risk_level,
            "suggested_reorder_qty": i.suggested_reorder_qty,
        }
        for i in items
    ]


# ==============================================================================
# Automation & Repricing Endpoints
# ==============================================================================


@app.get("/api/v1/automation/alerts", tags=["Automation"], summary="Query Triggered Alerts")
def get_alerts(
    store_id: int | None = Query(default=None),
    severity: str | None = Query(default=None),
    alert_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[dict[str, Any]]:
    """List operational alerts triggered by background monitors."""
    stmt = select(AlertLog).order_by(AlertLog.triggered_at.desc())
    if store_id is not None:
        stmt = stmt.where(AlertLog.store_id == store_id)
    if severity is not None:
        stmt = stmt.where(AlertLog.severity == severity.upper())
    if alert_type is not None:
        stmt = stmt.where(AlertLog.alert_type == alert_type.upper())

    stmt = stmt.limit(limit)
    rows = session.execute(stmt).scalars().all()
    return [
        {
            "id": r.id,
            "store_id": r.store_id,
            "sku_id": r.sku_id,
            "alert_type": r.alert_type,
            "severity": r.severity,
            "message": r.message,
            "acknowledged": r.acknowledged,
            "triggered_at": r.triggered_at.isoformat(),
        }
        for r in rows
    ]


@app.post("/api/v1/automation/alerts/run", tags=["Automation"], summary="Execute Alerts Run")
def trigger_alerts_run(
    store_id: int | None = Query(default=None),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Manually evaluate all inventory rules and trigger alerts."""
    anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
    mgr = AlertManager(session)
    triggered = mgr.run_all_checks(store_id=store_id, as_of_date=anchor_end)

    for a in triggered:
        ALERTS_TRIGGERED_TOTAL.labels(alert_type=a.alert_type, severity=a.severity).inc()

    return {
        "status": "COMPLETED",
        "alerts_triggered_count": len(triggered),
        "alerts": [
            {"id": a.id, "type": a.alert_type, "severity": a.severity, "message": a.message}
            for a in triggered
        ],
    }


@app.get(
    "/api/v1/automation/repricing/recommendations",
    tags=["Automation"],
    summary="Generate Repricing Recommendations",
)
def get_repricing_recommendations(
    store_id: int | None = Query(default=None),
    min_margin_pct: float | None = Query(default=None, ge=0.0, le=0.9),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[dict[str, Any]]:
    """Generate rule-based repricing recommendations respecting the margin floor."""
    anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
    engine = RepricingEngine(session)
    recs = engine.generate_recommendations(
        store_id=store_id, as_of_date=anchor_end, min_margin_pct=min_margin_pct
    )
    return [r.__dict__ for r in recs]


class ApplyRepricingRequest(BaseModel):
    sku_id: int
    suggested_price: float
    rule_applied: str = "MANUAL_APPROVAL"
    rationale: str = "Approved via API"


@app.post("/api/v1/automation/repricing/apply", tags=["Automation"], summary="Apply Repricing Action")
def apply_repricing_action(
    req: ApplyRepricingRequest,
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Commit a repricing recommendation to the database and log PriceHistory."""
    from sellerpulse.automation.repricing import RepricingRecommendation

    engine = RepricingEngine(session)
    rec = RepricingRecommendation(
        sku_id=req.sku_id,
        store_id=1,
        store_key="",
        sku_code="",
        title="",
        category="",
        cost_price=0.0,
        current_price=0.0,
        suggested_price=req.suggested_price,
        price_change=0.0,
        pct_change=0.0,
        current_margin_pct=0.0,
        projected_margin_pct=0.0,
        rule_applied=req.rule_applied,
        rationale=req.rationale,
    )
    success = engine.apply_recommendation(rec)
    if not success:
        raise HTTPException(status_code=404, detail=f"SKU id {req.sku_id} not found.")

    return {"status": "SUCCESS", "sku_id": req.sku_id, "new_price": req.suggested_price}


@app.post("/api/v1/automation/reports/generate", tags=["Automation"], summary="Trigger Report Generation")
def trigger_report_generation(
    store_id: int | None = Query(default=None),
    report_format: str = Query(default="both", regex="^(excel|pdf|both)$"),
    session: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Generate Excel and/or PDF operational reports on demand."""
    files = generate_reports(session, store_id=store_id, report_format=report_format)
    return {
        "status": "SUCCESS",
        "generated_files": {k: str(v) for k, v in files.items()},
    }
