# opencap container image: the Rust engine, one static-ish binary (ADR-0018).
#
#   docker build -t opencap .
#   docker run -d --name opencap --restart unless-stopped \
#     -v "$PWD/config:/opencap/config:ro" -v opencap-data:/opencap/data \
#     --env-file opencap.env -p 127.0.0.1:8080:8080 opencap
#
# The image runs `opencap-engine run` by default; any other subcommand works too:
#   docker run --rm -v "$PWD/config:/opencap/config:ro" opencap check
#
# Drivers are the engine's Cargo features (all by default); a smaller engine:
#   docker build --build-arg FEATURES="--no-default-features --features driver-modbus-tcp" -t opencap .
# See docs/deployment.md (volumes, secrets, logs, API binding).

# The build image's tag is not pinned by digest yet: resolve it with `docker buildx imagetools
# inspect rust:1-slim-bookworm` (the "Digest:" line) in a PR of its own, so that the release
# dry run in that PR rebuilds and smoke-tests both platforms (docs/release.md).
ARG RUST_IMAGE=rust:1-slim-bookworm
ARG RUNTIME_IMAGE=debian:bookworm-slim

# --- build ------------------------------------------------------------------------------------
# The build stage always runs on the builder's own architecture and cross-compiles for the target
# platform: a Rust build under QEMU emulation of arm64 takes over an hour, a cross build a few minutes.
FROM --platform=$BUILDPLATFORM ${RUST_IMAGE} AS build
ARG FEATURES=""
ARG BUILDARCH
ARG TARGETARCH
# a C compiler for the bundled SQLite and ring, and the arm64 cross toolchain on an x86_64 builder
# (the other way round, an arm64 builder for an amd64 image, is not supported)
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && if [ "$TARGETARCH" = arm64 ] && [ "$BUILDARCH" != arm64 ]; then \
      apt-get install -y --no-install-recommends gcc-aarch64-linux-gnu libc6-dev-arm64-cross; \
    fi \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# engine/ holds the sources and the web UI bundle (engine/ui); proto/ the gRPC and Sparkplug schemas
COPY engine ./engine
COPY proto ./proto
COPY LICENSE NOTICE ./
ENV CARGO_PROFILE_RELEASE_STRIP=true \
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc
# the binary lands in /out whatever the target is; `strip` is cargo's (CARGO_PROFILE_RELEASE_STRIP)
RUN case "$TARGETARCH" in \
      arm64) triple=aarch64-unknown-linux-gnu ;; \
      amd64) triple=x86_64-unknown-linux-gnu ;; \
      *) echo "unsupported TARGETARCH '$TARGETARCH'" >&2; exit 1 ;; \
    esac \
 && rustup target add "$triple" \
 && cargo build --release --locked --manifest-path engine/Cargo.toml -p opencap-engine ${FEATURES} --target "$triple" \
 && mkdir /out && cp "engine/target/$triple/release/opencap-engine" /out/

# --- runtime ----------------------------------------------------------------------------------
FROM ${RUNTIME_IMAGE}

LABEL org.opencontainers.image.title="opencap" \
      org.opencontainers.image.description="Open-source, read-only PLC connectivity server: polls PLCs and publishes to MQTT" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/sharmaniraj009/opencap"

# CA certificates: TLS to the MQTT broker (rustls reads the system store).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 opencap \
 && useradd --system --uid 10001 --gid opencap --home-dir /opencap --no-create-home \
            --shell /usr/sbin/nologin opencap

COPY --from=build /out/opencap-engine /usr/local/bin/opencap-engine
COPY LICENSE NOTICE /usr/share/doc/opencap/

# Relative paths in poller.yaml (saf.path: data/saf.sqlite) resolve from here.
WORKDIR /opencap
RUN mkdir -p config data reports && chown -R opencap:opencap /opencap
USER opencap

VOLUME ["/opencap/config", "/opencap/data"]
EXPOSE 8080

# Liveness only: /health/live answers while the process runs. Needs api.bind 0.0.0.0 or 127.0.0.1
# inside the container and api.port 8080 (set OPENCAP_HEALTH_URL otherwise).
ENV OPENCAP_HEALTH_URL=http://127.0.0.1:8080/health/live
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["opencap-engine", "healthcheck"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["opencap-engine"]
CMD ["run", "--config", "/opencap/config"]
