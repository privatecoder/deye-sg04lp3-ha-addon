# Build context is this directory only (SPEC §7). Base image tag pinned; the Supervisor no longer
# passes BUILD_FROM (2026.04.0), so the argument carries a default.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM ${BUILD_FROM}

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=0.1.0

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-editable \
    && rm -rf /app/src

COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/deye-monitor/run /etc/s6-overlay/s6-rc.d/deye-monitor/finish

# The container runs as root: s6-overlay's /init requires it, /data is root-owned and serial
# devices (uart) are mapped by the Supervisor with root-only access.
# App identity. The base image carries the docker-base project's OCI source/version labels; they
# are overridden here so that inventory tools see this app. The base provenance stays available
# as io.hass.base.* (inherited). Replace the repository placeholder before publishing.
ARG BUILD_SOURCE=https://github.com/privatecoder/deye-sg04lp3-ha-addon
LABEL \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Deye SG04LP3 Monitor" \
    org.opencontainers.image.description="Read-only monitoring of Deye SUN-xK-SG04LP3 inverters" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="${BUILD_SOURCE}" \
    org.opencontainers.image.url="${BUILD_SOURCE}" \
    org.opencontainers.image.documentation="${BUILD_SOURCE}#readme" \
    org.opencontainers.image.version="${BUILD_VERSION}"
