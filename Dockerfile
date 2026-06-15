FROM python:3.11-slim

WORKDIR /app

# System deps: Playwright/Chromium requires these libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 \
    # General utils (curl powers the HEALTHCHECK)
    git curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir setuptools wheel

# Copy source. NOTE: prompts/ and schemas/ are load-bearing at runtime —
# orchestrator.py / subagent.py read prompts/*.md, presets.py reads
# schemas/preset.schema.yaml. Omitting them crashes goals + presets.
COPY *.py ./
COPY skills/ skills/
COPY static/ static/
COPY migrations/ migrations/
COPY prompts/ prompts/
COPY schemas/ schemas/

# Install package + Playwright
RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir playwright && \
    python -m playwright install chromium

# All persistent state lives under CASTOR_DATA_DIR — mount a volume here so the
# SQLite db, Qdrant vectors (memory/), wiki/, skills/, uploads/, presets/ and
# logs survive container restarts / image upgrades.
ENV CASTOR_DATA_DIR=/data \
    CASTOR_QDRANT_MODE=disk \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 7860

# Liveness: the web server answers /api/status once booted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:7860/api/status || exit 1

# `castor` is the console script declared in pyproject [project.scripts].
CMD ["castor", "--web", "--host", "0.0.0.0", "--port", "7860"]
