# opencap container image.
#
#   docker build -t opencap .
#   docker run -d --name opencap --restart unless-stopped \
#     -v "$PWD/config:/opencap/config:ro" -v opencap-data:/opencap/data \
#     --env-file opencap.env -p 127.0.0.1:8080:8080 opencap
#
# The image runs `opencap run` by default; any other subcommand works too:
#   docker run --rm -v "$PWD/config:/opencap/config:ro" opencap check
#
# Build with other drivers: --build-arg EXTRAS=modbus,ab,north,s7
# See docs/deployment.md (volumes, secrets, logs, API binding).

ARG PYTHON_IMAGE=python:3.12-slim

# --- build: wheels for opencap and its dependencies -------------------------------------------
FROM ${PYTHON_IMAGE} AS build
ARG EXTRAS=modbus,ab,north
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[${EXTRAS}]"

# --- runtime ----------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ARG EXTRAS=modbus,ab,north

LABEL org.opencontainers.image.title="opencap" \
      org.opencontainers.image.description="Open-source, read-only PLC connectivity server: polls PLCs and publishes to MQTT" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/sharmaniraj009/opencap"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: the payload's DateTime uses payload.timezone (zoneinfo needs a tz database).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 opencap \
 && useradd --system --uid 10001 --gid opencap --home-dir /opencap --no-create-home \
            --shell /usr/sbin/nologin opencap

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels "opencap[${EXTRAS}]" \
 && rm -rf /wheels

# Relative paths in poller.yaml (saf.path: data/saf.sqlite) resolve from here.
WORKDIR /opencap
RUN mkdir -p config data reports && chown -R opencap:opencap /opencap
USER opencap

VOLUME ["/opencap/config", "/opencap/data"]
EXPOSE 8080

# Liveness only: /health answers while the process runs. Needs api.bind 0.0.0.0 or 127.0.0.1
# inside the container and api.port 8080 (set OPENCAP_HEALTH_URL otherwise).
ENV OPENCAP_HEALTH_URL=http://127.0.0.1:8080/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(os.environ['OPENCAP_HEALTH_URL'], timeout=4)"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["opencap"]
CMD ["run", "--config", "/opencap/config"]
