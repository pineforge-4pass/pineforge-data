# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89

ARG PINEFORGE_RELEASE_IMAGE=ghcr.io/pineforge-4pass/pineforge-release:0.1.12@sha256:312b9d908390b828484617472c749d5815feb75507da87eae2f6902cfe3d47b1

FROM python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361 AS server-builder
RUN python -m pip install --no-cache-dir --target /opt/pineforge-server-deps \
    'fastapi>=0.139,<0.140' \
    'uvicorn>=0.51,<0.52'

FROM ${PINEFORGE_RELEASE_IMAGE}
ARG PINEFORGE_RELEASE_IMAGE

USER root
COPY --from=server-builder /opt/pineforge-server-deps /opt/pineforge-server-deps
COPY src/pineforge_data /opt/pineforge-server/pineforge_data
RUN mkdir -p /cache \
    && chown 10001:10001 /cache

ENV PYTHONPATH=/opt/pineforge-server:/opt/pineforge-server-deps:/opt/pineforge/pycodegen \
    PINEFORGE_RELEASE_IMAGE=${PINEFORGE_RELEASE_IMAGE} \
    PINEFORGE_RELEASE_ENTRYPOINT=/opt/pineforge/bin/entrypoint.sh \
    PINEFORGE_RELEASE_RUN_JSON=/opt/pineforge/bin/run_json.py \
    PINEFORGE_SERVER_CACHE_DIR=/cache \
    PINEFORGE_SERVER_CONCURRENCY=2 \
    PINEFORGE_SERVER_MAX_QUEUE=8 \
    PINEFORGE_SERVER_QUEUE_TIMEOUT=30 \
    PINEFORGE_SERVER_EXECUTION_TIMEOUT=300 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="pineforge-data-backtest-server" \
      org.opencontainers.image.description="Concurrent FastAPI service backed by pineforge-release" \
      org.opencontainers.image.source="https://github.com/pineforge-4pass/pineforge-data" \
      org.opencontainers.image.licenses="Apache-2.0"

VOLUME ["/cache"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).read()"]

USER 10001
ENTRYPOINT ["python3", "-m", "uvicorn"]
CMD ["pineforge_data.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
