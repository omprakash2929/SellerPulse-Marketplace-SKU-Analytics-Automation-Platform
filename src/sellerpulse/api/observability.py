"""Prometheus metrics instrumentation and operational health checks."""

import time
from typing import Any

from fastapi import Request, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
except ImportError:
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    generate_latest = lambda: b""  # noqa: E731
    Counter = Any  # type: ignore
    Histogram = Any  # type: ignore

# Prometheus Metrics Definitions
HTTP_REQUESTS_TOTAL = Counter(
    "sellerpulse_http_requests_total",
    "Total HTTP requests received by API",
    ["method", "endpoint", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "sellerpulse_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
)

ETL_RUNS_TOTAL = Counter(
    "sellerpulse_etl_runs_total",
    "Total ETL sync executions",
    ["status"],
)

ETL_DURATION_SECONDS = Histogram(
    "sellerpulse_etl_duration_seconds",
    "Duration of ETL pipeline syncs in seconds",
)

ALERTS_TRIGGERED_TOTAL = Counter(
    "sellerpulse_alerts_triggered_total",
    "Total inventory and operational alerts generated",
    ["alert_type", "severity"],
)


class HealthResponse(BaseModel):
    """Health check response payload."""

    status: str
    database: str
    uptime_seconds: float
    version: str
    environment: str


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Starlette middleware recording HTTP latency and status codes to Prometheus."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # Avoid instrumenting the metrics endpoint itself
        if request.url.path == "/metrics":
            return await call_next(request)

        start_time = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start_time

        endpoint = request.url.path
        method = request.method
        status = str(response.status_code)

        HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status_code=status).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method, endpoint=endpoint).observe(duration)

        return response
