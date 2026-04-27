# ============================================================
# Agentic Data Platform — Production Docker Image
# Multi-stage build: Generate data → Runtime
# ============================================================

# --- Runtime ---
FROM python:3.12-slim AS runtime

LABEL maintainer="Weichao Wang <weichao@agentic-data.io>"
LABEL description="Agentic Data Platform — Multi-Agent Data Analysis"

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY config.py auth.py ./
COPY api/ api/
COPY agentic_core/ agentic_core/
COPY web/ web/

# Data directory (empty — populated at runtime from connected sources)
RUN mkdir -p data sample_data/output

# Non-root user for security
RUN useradd -r -s /bin/false appuser && chown -R appuser:appuser /app

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    AGENTIC_AUTO_PORT=8501 \
    AGENTIC_AUTO_HOST=0.0.0.0

USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8501/ || exit 1

CMD ["python3", "-m", "uvicorn", "api.main:app", \
     "--host", "0.0.0.0", "--port", "8501", \
     "--workers", "2", "--timeout-keep-alive", "65", \
     "--limit-concurrency", "50"]
