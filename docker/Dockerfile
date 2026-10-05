# Multi-stage production Dockerfile for SellerPulse
# Stage 1: Build wheels and compile dependencies
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel --outdir /build/dist

# Stage 2: Final lightweight runtime container
FROM python:3.11-slim AS runner

WORKDIR /app

# Install runtime dependencies including curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged system user
RUN groupadd -r sellerpulse && useradd -r -g sellerpulse -d /app -s /sbin/nologin sellerpulse

# Copy wheel from builder stage and install
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# Copy source for dashboard and scripts
COPY src/ /app/src/

# Create persistent storage directories and assign permissions
RUN mkdir -p /app/data /app/reports_out \
    && chown -R sellerpulse:sellerpulse /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL="sqlite:////app/data/sellerpulse.db" \
    REPORTS_DIR="/app/reports_out"

USER sellerpulse

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["sellerpulse"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
