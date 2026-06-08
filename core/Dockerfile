# ==============================================================================
# ReCoder File Template: Dockerfile.python-flask
# Production-grade Dockerfile for Python / Flask applications
# Placeholders: python-flask-app, 3.9, 5000, app
# ==============================================================================

# ---- Build stage ----
FROM python:3.9-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt

# ---- Runtime stage ----
FROM python:3.9-slim AS runtime

LABEL org.opencontainers.image.title="python-flask-app" \
      org.opencontainers.image.description="Flask application" \
      org.opencontainers.image.version="1.0"

# Security: run as non-root
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/sh --create-home appuser

# Install runtime OS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app

COPY --chown=appuser:appgroup . .

USER appuser

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Use gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
