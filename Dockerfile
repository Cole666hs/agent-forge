# syntax=docker/dockerfile:1.7
# AgentForge production image.
#
# Build:    docker build -t agentforge:latest .
# Run:      docker run --rm -p 8766:8766 -v ./data:/app/data agentforge:latest
# Compose:  docker compose up
#
# The image is intentionally minimal: just Python 3.11-slim + agentforge.
# No C extensions in the runtime path, so no build-essential layer.
#
# v0.17.0 polish: explicit non-root USER, /app/data owned by the same
# user, ENTRYPOINT/CMD split so `docker run agentforge --help` works
# without quoting the subcommand. HEALTHCHECK uses the daemon's /readyz
# endpoint so `docker ps` shows health accurately.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENTFORGE_BIND_HOST=0.0.0.0 \
    AGENTFORGE_PORT=8766 \
    AGENTFORGE_LOG_LEVEL=INFO \
    AGENTFORGE_LOG_FORMAT=json

WORKDIR /app

# Install dependencies first (better Docker layer caching: pyproject
# rarely changes, source changes every commit).
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Drop privileges. UID/GID 10001 is the agentforge user.
RUN useradd --create-home --shell /bin/bash --uid 10001 agentforge \
 && mkdir -p /app/data /app/workflows /app/mailbox \
 && chown -R agentforge:agentforge /app
USER agentforge

EXPOSE 8766

# Healthcheck via the /readyz endpoint (returns 200/503).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8766/readyz', timeout=3); exit(0 if r.status == 200 else 1)" || exit 1

ENTRYPOINT ["agentforge"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8766"]
