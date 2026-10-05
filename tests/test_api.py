"""Unit tests for FastAPI authentication, metrics, and REST endpoint routes."""

import pytest
from fastapi.testclient import TestClient

from sellerpulse.api.main import app
from sellerpulse.config import get_settings
from sellerpulse.db.session import get_db


@pytest.fixture
def client(populated_session):
    """Provide TestClient with database session overridden to test database."""
    def _override_get_db():
        yield populated_session

    app.dependency_overrides[get_db] = _override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    settings = get_settings()
    return {"X-API-Key": settings.API_KEY}


def test_public_health_and_metrics_endpoints(client):
    # Health endpoint
    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    data = resp_health.json()
    assert data["status"] == "ok"
    assert data["database"] == "healthy"
    assert "version" in data

    # Metrics endpoint
    resp_metrics = client.get("/metrics")
    assert resp_metrics.status_code == 200
    assert "sellerpulse_http_requests_total" in resp_metrics.text


def test_api_authentication(client, auth_headers):
    # Missing API Key -> 401
    resp_missing = client.get("/api/v1/stores")
    assert resp_missing.status_code == 401

    # Invalid API Key -> 401
    resp_invalid = client.get("/api/v1/stores", headers={"X-API-Key": "wrong_key_123"})
    assert resp_invalid.status_code == 401

    # Valid API Key -> 200
    resp_valid = client.get("/api/v1/stores", headers=auth_headers)
    assert resp_valid.status_code == 200
    stores = resp_valid.json()
    assert len(stores) == 3


def test_stores_and_summary_endpoint(client, auth_headers):
    resp = client.get("/api/v1/stores/1/summary?days=90", headers=auth_headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["store_id"] == 1
    assert "kpis" in payload
    assert payload["kpis"]["total_skus"] > 0


def test_analytics_endpoints(client, auth_headers):
    # SKU Metrics
    resp_metrics = client.get("/api/v1/analytics/sku-metrics?limit=10", headers=auth_headers)
    assert resp_metrics.status_code == 200
    metrics_data = resp_metrics.json()
    assert "items" in metrics_data
    assert metrics_data["total"] > 0

    # ABC Analysis
    resp_abc = client.get("/api/v1/analytics/abc", headers=auth_headers)
    assert resp_abc.status_code == 200
    abc_data = resp_abc.json()
    assert "summary" in abc_data
    assert "items" in abc_data

    # Dead Stock
    resp_dead = client.get("/api/v1/analytics/dead-stock", headers=auth_headers)
    assert resp_dead.status_code == 200
    dead_data = resp_dead.json()
    assert "summary" in dead_data

    # Stockout Risk
    resp_stockout = client.get("/api/v1/analytics/stockout-risk?days_threshold=30", headers=auth_headers)
    assert resp_stockout.status_code == 200
    assert isinstance(resp_stockout.json(), list)


def test_automation_endpoints(client, auth_headers):
    # Trigger alerts run
    resp_run = client.post("/api/v1/automation/alerts/run", headers=auth_headers)
    assert resp_run.status_code == 200
    assert resp_run.json()["status"] == "COMPLETED"

    # Query alerts
    resp_get = client.get("/api/v1/automation/alerts", headers=auth_headers)
    assert resp_get.status_code == 200
    assert len(resp_get.json()) >= 0

    # Repricing recommendations
    resp_reprice = client.get("/api/v1/automation/repricing/recommendations", headers=auth_headers)
    assert resp_reprice.status_code == 200
    recs = resp_reprice.json()
    assert isinstance(recs, list)

    if recs:
        target = recs[0]
        apply_payload = {
            "sku_id": target["sku_id"],
            "suggested_price": target["suggested_price"],
            "rule_applied": target["rule_applied"],
            "rationale": "Test API apply",
        }
        resp_apply = client.post("/api/v1/automation/repricing/apply", json=apply_payload, headers=auth_headers)
        assert resp_apply.status_code == 200
        assert resp_apply.json()["status"] == "SUCCESS"

    # Generate reports
    resp_report = client.post("/api/v1/automation/reports/generate?report_format=both", headers=auth_headers)
    assert resp_report.status_code == 200
    assert resp_report.json()["status"] == "SUCCESS"
