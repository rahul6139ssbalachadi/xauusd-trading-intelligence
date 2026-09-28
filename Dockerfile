# syntax=docker/dockerfile:1
# =============================================================================
# XAUUSD Trading Intelligence — API + worker image
# =============================================================================
# Two roles, one image, selected by CMD:
#   api    -> uvicorn api.main:app
#   worker -> python worker.py
#
# WHAT IS NOT IN THIS IMAGE, AND WHY
#   MetaTrader5 is NOT installed. It is a Windows-only package and this image
#   is Linux. The execution adapter therefore defaults to "null": signals are
#   computed and displayed, no orders can be placed. That is the correct and
#   intended behaviour for a server.
#
#   The MT5 terminal, if you use one at all, lives on a Windows machine. See
#   docs/MT5_ARCHITECTURE.md.
#
# No Java. No JVM. No Gradle. No external database server.
# =============================================================================

FROM python:3.11-slim-bookworm

# Non-root from here on. A trading system should not run as root in a
# container, and this costs nothing to get right from the start.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl is used by the compose healthcheck. ca-certificates by pip over HTTPS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer caches across code edits.
COPY requirements.txt requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-api.txt

# Application code. The database and journals are volumes, not image content.
COPY appconfig/ ./appconfig/
COPY api/ ./api/
COPY execution/ ./execution/
COPY market_data/ ./market_data/
COPY indicators/ ./indicators/
COPY market_structure/ ./market_structure/
COPY strategy/ ./strategy/
COPY backtest/ ./backtest/
COPY validation/ ./validation/
COPY risk/ ./risk/
COPY montecarlo/ ./montecarlo/
COPY candles/ ./candles/
COPY paper/ ./paper/
COPY reporting/ ./reporting/
COPY self_improvement/ ./self_improvement/
COPY monthly_bt/ ./monthly_bt/
COPY research/ ./research/
COPY scripts/ ./scripts/
COPY runner/ ./runner/
COPY db/schema/ ./db/schema/
COPY config/ ./config/
COPY reports/ ./reports/
COPY dashboard/ ./dashboard/
COPY mql5/ ./mql5/
COPY worker.py health_checks.py observability.py conftest.py ./

# Writable state. Mount volumes over these in production; creating them with
# the right ownership means an unprivileged bind mount also works.
RUN useradd --create-home --uid 10001 trader \
 && mkdir -p /app/db /app/run/control /app/logs \
 && chown -R trader:trader /app
USER trader

ENV API_HOST=0.0.0.0 \
    API_PORT=8000 \
    LOG_FORMAT=json \
    LOG_FILE= \
    EXECUTION_ADAPTER=null

EXPOSE 8000

# The API is the container's purpose. Override CMD for the worker.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -fsS "http://localhost:${API_PORT}/health" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
