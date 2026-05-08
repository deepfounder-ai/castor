FROM python:3.11-slim

WORKDIR /app

# System deps: Playwright/Chromium requires these libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 \
    # General utils
    git curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir setuptools wheel

# Copy source
COPY *.py ./
COPY skills/ skills/
COPY static/ static/
COPY migrations/ migrations/

# Install package + Playwright
RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir playwright && \
    python -m playwright install chromium

# Create runtime directories (fallback; real data goes to ~/.qwe-qwe via DATA_DIR)
RUN mkdir -p logs memory skills uploads

EXPOSE 7860

CMD ["qwe-qwe", "--web"]
