# syntax=docker/dockerfile:1.7
# AgentForge production image. Multi-stage not strictly needed (no C deps),
# but the slim base keeps the image < 200MB.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (better Docker layer caching)
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Drop privileges
RUN useradd --create-home --shell /bin/bash agentforge \
 && mkdir -p /app/data && chown -R agentforge:agentforge /app
USER agentforge

# Defaults — override in docker-compose or `docker run -e ...`
ENV AGENTFORGE_BIND_HOST=0.0.0.0 \
    AGENTFORGE_PORT=8766 \
    AGENTFORGE_LOG_LEVEL=INFO \
    AGENTFORGE_LOG_FORMAT=json

EXPOSE 8766

# Healthcheck via the /readyz endpoint (returns 200/503)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8766/readyz', timeout=3); exit(0 if r.status == 200 else 1)" || exit 1

ENTRYPOINT ["agentforge"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8766"]
