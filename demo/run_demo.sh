#!/usr/bin/env bash
# One-command opencap demo: two simulated devices -> the real poller -> a local MQTT broker.
#
#   demo/run_demo.sh            # needs: opencap installed (pip install -e '.[north]') and mosquitto
#
# Starts a throwaway Mosquitto on 127.0.0.1:1883 if nothing listens there, runs `opencap run` on
# demo/config (no PLC needed: the `simulator` driver computes values), and prints what to watch.
# Ctrl+C stops everything it started. Read-only by design: nothing here writes to any device.
set -euo pipefail
cd "$(dirname "$0")"

command -v opencap >/dev/null || { echo "opencap not on PATH: pip install -e '.[north]' from the repo root first" >&2; exit 1; }
started=()
cleanup() { for pid in "${started[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

if ! (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null; then
  command -v mosquitto >/dev/null || { echo "no broker on 127.0.0.1:1883 and mosquitto is not installed (apt install mosquitto)" >&2; exit 1; }
  conf="$(mktemp)"; printf 'listener 1883 127.0.0.1\nallow_anonymous true\n' > "$conf"
  mosquitto -c "$conf" >/dev/null 2>&1 & started+=("$!")
  sleep 1
  echo "started a local MQTT broker on 127.0.0.1:1883"
fi

export OPENCAP_API_TOKEN="${OPENCAP_API_TOKEN:-demo-token}"
mkdir -p data
cat <<MSG

opencap demo is starting. In other terminals:
  mosquitto_sub -h 127.0.0.1 -t 'opencap/#' -v                 # every message, one SubDevice JSON per device
  curl -s localhost:8080/health                                # status, MQTT link, queue depth
  curl -s -H "Authorization: Bearer $OPENCAP_API_TOKEN" localhost:8080/api/v1/devices
  open http://127.0.0.1:8080/ui/                               # the web UI (token: $OPENCAP_API_TOKEN)
Try it: stop the broker for a minute (pkill mosquitto), watch saf_queue climb in /health, restart it,
and see the whole backlog arrive in order. Ctrl+C here to stop.

MSG
opencap run --config config
