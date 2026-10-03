# syntax=docker/dockerfile:1

# =============================================================================
# Medi Live — single-image deployment (client + server + ASR weights + TLS)
#
# Two stages, one final image:
#   1. client  — Node builds the React app into static files
#   2. runtime — Python runs the ASR server and serves those static files
#
# Why multi-stage: node_modules + the build toolchain (~500MB) never reach the
# final image, so it only ships what the server actually runs.
#
# Why the weights are baked in: a new machine needs nothing but Docker. The
# 438MB model.onnx lives in server/model/ (a git submodule with LFS), so it
# MUST be present before `docker build` — see README.
# =============================================================================

# ---------------------------------------------------------------- 1. client
FROM node:24-alpine AS client
WORKDIR /app

# pnpm 11 matches the lockfile (lockfileVersion 9) and pnpm-workspace.yaml's
# allowBuilds, which is what lets esbuild run its install script.
RUN corepack enable && corepack prepare pnpm@11 --activate

# Dependencies first: this layer is cached and only invalidated when the
# lockfile changes, not on every source edit.
COPY client/package.json client/pnpm-lock.yaml client/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY client/ ./
# No HTTPS cert or VITE_* overrides are baked in — the image serves the client
# over the same origin as the API, so the defaults in client/src/lib/api.js
# (relative URLs on https pages) are exactly right.
RUN pnpm build && test -f dist/index.html


# -------------------------------------------------------------- 2. runtime
FROM python:3.13-slim

# Debian + PyPI mirrors. Defaults target Iranian networks (reachable from here
# while deb.debian.org / pypi.org time out); override on the build command or in
# docker-compose.yml to use the official upstreams instead:
#   docker compose build --build-arg DEBIAN_MIRROR=http://deb.debian.org/debian \
#                        --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

# PYTHONUNBUFFERED: without it print() is block-buffered in a container and
# `docker logs` shows nothing until the buffer fills. PIP_NO_CACHE_DIR keeps
# pip's download cache out of the image layers.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8443 \
    DATA_DIR=/data \
    APP_DB=/data/app.db \
    CERT_DIR=/certs \
    CLIENT_DIST=/app/dist

# Point apt at the mirror before installing. python:3.13-slim (trixie) uses the
# deb822 /etc/apt/sources.list.d/debian.sources format, so we rewrite the URIs
# in place instead of writing a legacy sources.list.
RUN set -eux; \
    sed -i "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g; \
            s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    echo "Acquire::Retries \"5\";" > /etc/apt/apt.conf.d/80-retries; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      libportaudio2 \
      libsndfile1 \
      curl; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before code: rebuilds after a code edit reuse this layer, so only
# the fast COPY below re-runs instead of a 2-minute pip install.
COPY server/requirements.txt ./requirements.txt
RUN pip install --index-url "$PIP_INDEX_URL" -r requirements.txt

# Code + ASR weights + per-class glossary. .dockerignore keeps archive/,
# __pycache__ and .git out of this.
COPY server/ /app/server/
COPY --from=client /app/dist /app/dist
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /data /certs

# Documentation only — it opens nothing. The port is published by compose.
EXPOSE 8443

# Generates the TLS certificate on first boot, then execs uvicorn (exec = the
# server becomes PID 1 and receives SIGTERM directly, so `docker stop` shuts it
# down cleanly instead of waiting 10s for a kill).
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]