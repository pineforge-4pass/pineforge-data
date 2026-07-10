# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89

FROM debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df AS engine-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake \
        g++ \
        libeigen3-dev \
        ninja-build \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/pineforge-engine /src/pineforge-engine

RUN cmake -G Ninja -S /src/pineforge-engine -B /tmp/pineforge-build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/pineforge \
        -DPINEFORGE_BUILD_TESTS=OFF \
        -DPINEFORGE_BUILD_TUTORIAL=OFF \
        -DPINEFORGE_BUILD_CORPUS_STRATEGIES=OFF \
    && cmake --build /tmp/pineforge-build --target pineforge -j2 \
    && cmake --install /tmp/pineforge-build

FROM python:3.13-slim-bookworm@sha256:fcbd8dfc2605ba7c2eca646846c5e892b2931e41f6227985154a596f26ab8ed7

ARG ENGINE_SHA=unknown
ARG CODEGEN_SHA=unknown
ARG DATA_SOURCE_DIGEST=unknown

LABEL org.opencontainers.image.title="pineforge-data-backtest" \
      org.opencontainers.image.description="Raw PineScript and provider OHLCV to PineForge report" \
      org.opencontainers.image.source="https://github.com/pineforge-4pass/pineforge-data" \
      org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-PolyForm-Noncommercial-1.0.0" \
      io.pineforge.engine.commit="${ENGINE_SHA}" \
      io.pineforge.codegen.commit="${CODEGEN_SHA}" \
      io.pineforge.data.source-digest="${DATA_SOURCE_DIGEST}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        libeigen3-dev \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man

COPY --from=engine-builder /opt/pineforge /opt/pineforge
COPY vendor/pineforge-codegen-oss/pineforge_codegen /opt/pineforge-codegen/pineforge_codegen
COPY vendor/pineforge-codegen-oss/VERSION /opt/pineforge-codegen/VERSION
COPY src/pineforge_data /opt/pineforge-data/pineforge_data
COPY docker/entrypoint.py /opt/pineforge/bin/pineforge-data-backtest
COPY LICENSE /opt/licenses/pineforge-data.LICENSE
COPY vendor/pineforge-engine/LICENSE /opt/licenses/pineforge-engine.LICENSE
COPY vendor/pineforge-codegen-oss/LICENSE /opt/licenses/pineforge-codegen.LICENSE

RUN printf '%s\n' "${ENGINE_SHA}" > /opt/pineforge/ENGINE_COMMIT \
    && printf '%s\n' "${CODEGEN_SHA}" > /opt/pineforge/CODEGEN_COMMIT \
    && printf '%s\n' "${DATA_SOURCE_DIGEST}" > /opt/pineforge/DATA_SOURCE_DIGEST \
    && chmod +x /opt/pineforge/bin/pineforge-data-backtest \
    && useradd --system --uid 10001 --shell /usr/sbin/nologin pineforge

ENV PYTHONPATH=/opt/pineforge-data:/opt/pineforge-codegen \
    PINEFORGE_PREFIX=/opt/pineforge \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001
ENTRYPOINT ["/opt/pineforge/bin/pineforge-data-backtest"]
