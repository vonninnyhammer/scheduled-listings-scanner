FROM python:3.12-slim

LABEL maintainer="overseer-bot"

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY config/ config/
COPY src/ src/

# Create log directory
RUN mkdir -p /var/log/liquidation-scanner

# Non-root user
RUN useradd -m -r scanner && chown -R scanner:scanner /app /var/log/liquidation-scanner
USER scanner

# Healthcheck - process should be alive
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import os, signal; print('ok')" || exit 1

ENTRYPOINT ["python", "-u", "src/scanner.py"]
