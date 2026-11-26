# Deploying opencap

opencap is one long-running process: `opencap run`. It needs:

- **a config directory**: `poller.yaml`, `devices.json`, `devices_map.yaml`, `overrides.yaml`.
  It reads these files and never writes them. Only `opencap import-kep` writes.
- **a writable working directory** for the store-and-forward outbox. `saf.path` defaults to
  `data/saf.sqlite`, relative to the working directory. Keep it on persistent storage: it
  holds every message the broker has not acknowledged yet.
- **secrets in environment variables**: `poller.yaml` only names the variables
  (`mqtt.password_env`, `api.token_env`). The template is
  [`deploy/opencap.env.example`](../deploy/opencap.env.example).
- **network access** to the PLCs, the MQTT broker and, if you use it, the admin API port.

Pick one of the three ways below. Each restarts opencap when it fails, sends the JSON logs to
the platform's log system, and stops it cleanly. On a clean stop opencap publishes `offline`
on its LWT topic and closes PLC connections.

## Docker

The [`Dockerfile`](../Dockerfile) builds on `python:3.12-slim`, adds `tzdata`, and runs as
the non-root user `opencap` (uid 10001) from `/opencap`. The entrypoint is `opencap`, and
the default command is `run --config /opencap/config`.

```bash
docker build -t opencap .                                   # drivers: modbus, ab, serial lines (+ north side)
docker build --build-arg EXTRAS=modbus,ab,north,serial,s7 -t opencap .   # more drivers
```

Volumes:

| Path in the container | Content | Mount |
|---|---|---|
| `/opencap/config` | config files | read-only bind mount (`:ro`); read-write and owned by uid 10001 if you edit the config in the web UI or the config API |
| `/opencap/data` | `saf.sqlite` outbox | named volume or a directory owned by uid 10001 |

```bash
docker run -d --name opencap --restart unless-stopped \
  -v "$PWD/config:/opencap/config:ro" \
  -v opencap-data:/opencap/data \
  --env-file /etc/opencap/opencap.env \
  -p 127.0.0.1:8080:8080 \
  opencap
```

- **Admin API inside a container:** `api.bind: 127.0.0.1` (the default) is reachable only
  from inside the container. To publish the port, set `api.bind: 0.0.0.0` in `poller.yaml`
  **and** set `OPENCAP_API_TOKEN`. opencap refuses to bind a non-loopback address without a
  token. Publish the port only on the host's loopback or VPN address, as above.
- **Editing the config in the web UI or the config API** needs a writable config mount owned
  by the container user: `sudo chown -R 10001:10001 config` and `-v "$PWD/config:/opencap/config"`
  (no `:ro`). With `:ro` the UI shows editing as off and config writes answer 503 with the path.
  The audit log of config changes goes to `data/audit.jsonl`, in the data volume.
- **Importing a KEPServerEX project in the web UI** stages the importer's output in
  `<config dir>/.import-staging/` (0700) for up to an hour; the export itself is deleted as soon as it is read and
  credentials are never kept (D-087). If you keep the config directory in git, ignore that folder.
- **Quick Client (live values)** uses the WebSocket `/api/v1/ws` on the same port. A reverse proxy in front must
  pass the WebSocket upgrade (`Upgrade` and `Connection` headers) and keep the `Host` header, or list the public
  origin in `api.cors_origins`: browsers may open the feed only from the API's own origin (D-067).
- **Local serial ports** (`modbus_rtu` and `ab_df1` with `transport: serial`): the default image includes the
  `serial` extra (pyserial). Pass the device into the container and give the container user access to it.
  The user is uid 10001, not in the host's `dialout` group, so add the group's id:
  `docker run ... --device /dev/ttyUSB0:/dev/ttyUSB0 --group-add "$(getent group dialout | cut -d: -f3)"`.
  Use a stable name (`/dev/serial/by-id/...`) on the host side, and keep the path in `host` / `serial_ports`
  the same as the container path. Serial-over-TCP gateways (`transport: tcp`, `rfc2217`) need no device.
- **Health check:** the image probes `http://127.0.0.1:8080/health`. If you change
  `api.port`, set `OPENCAP_HEALTH_URL`.
- **Other commands** run the same way:
  `docker run --rm -v "$PWD/config:/opencap/config:ro" opencap check`. For `import-kep` the
  config mount must be writable by uid 10001.
- **Reload** without a restart: `docker kill --signal HUP opencap`.
- **Stop:** `docker stop` sends SIGTERM. opencap stops within a few seconds, well inside
  Docker's default 10 s grace period.

## Linux: systemd

[`deploy/opencap.service`](../deploy/opencap.service) expects:

| Path | Content |
|---|---|
| `/opt/opencap/venv` | a virtualenv with opencap installed |
| `/etc/opencap/` | config files |
| `/etc/opencap/opencap.env` | secrets (mode 0640, group `opencap`) |
| `/var/lib/opencap/` | working directory; `data/saf.sqlite` lives here (`StateDirectory=`) |

```bash
sudo useradd --system --home-dir /var/lib/opencap --shell /usr/sbin/nologin opencap
sudo python3.12 -m venv /opt/opencap/venv
sudo /opt/opencap/venv/bin/pip install "opencap[modbus,ab,north] @ git+https://github.com/sharmaniraj009/opencap"
# sticky directory: opencap may replace its config files, never root's opencap.env
sudo install -d -m 1775 -o root -g opencap /etc/opencap
sudo install -m 0640 -o opencap -g opencap config/*.yaml config/devices.json /etc/opencap/
sudo install -m 0640 -o root -g opencap deploy/opencap.env.example /etc/opencap/opencap.env
sudoedit /etc/opencap/opencap.env
sudo cp deploy/opencap.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opencap
```

- `Restart=on-failure` with `RestartSec=5` and no start limit. A clean stop
  (`systemctl stop`) is not restarted.
- `systemctl reload opencap` sends SIGHUP, which re-reads the config and restarts only the
  channels that changed (`POST /reload` does the same).
- The unit is hardened (`ProtectSystem=strict`, no capabilities, `PrivateDevices=yes`). The
  serial drivers need the port: the comment at the end of the unit shows what to change.
- **Config editing (web UI, config API):** the unit sets `ReadWritePaths=/etc/opencap`, and the
  commands above make the directory sticky (`1775`, `root:opencap`) with the config files owned
  by `opencap`. opencap can then replace its config files atomically and keep `.bak` copies,
  but cannot replace or delete `opencap.env`, which stays root's. The audit log goes to
  `/var/lib/opencap/data/audit.jsonl`. For a read-only configuration, remove `ReadWritePaths=`
  from the unit and keep `/etc/opencap` and its files root-owned (`0755`/`0644`): the UI then
  shows editing as off, and config writes answer 503 with the path.

## Waiting for readiness

`GET /health/live` answers 200 while the process serves requests. `GET /health/ready` answers 200 once
the config is loaded, the north servers are started and every enabled device has finished one poll
cycle (a device that failed its first cycle is `offline` and counts, a demoted one too); until then it
answers 503 with `{"ready": false, "waiting": ["<channel>/<device>", ...]}`. Both are open (no token).
Readiness is sticky: once reached it stays until a restart. `/health` also shows `initial_scan_complete`
and `initial_scan_ms`. The OPC UA server is `Running` from the start, so clients may connect earlier.

A SCADA start script (Linux):

```bash
until curl -fsS http://127.0.0.1:8080/health/ready >/dev/null; do sleep 2; done
```

On Windows (PowerShell), with a limit of two minutes:

```powershell
$deadline = (Get-Date).AddMinutes(2)
while ((Get-Date) -lt $deadline) {
  try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/health/ready | Out-Null; break }
  catch { Start-Sleep -Seconds 2 }
}
```

A systemd unit that must start after opencap has its first values:

```ini
[Service]
ExecStartPre=/usr/bin/curl -fsS --retry 60 --retry-delay 2 --retry-connrefused http://127.0.0.1:8080/health/ready
```

A container health check can use `/health/live` (the image probes `/health`); a readiness probe in an
orchestrator uses `/health/ready`.

`opencap info` prints a support summary (versions, platform, drivers, north interfaces, config revision)
that is safe to paste into an issue: no addresses, host names or secrets. The same data is at
`GET /api/v1/support` (viewer token); `GET /api/v1/about` (open) has only the product and version.

## Logs and log rotation

opencap writes **one JSON object per line to stdout**: `timestamp` (ISO-8601 UTC), `level`,
`event`, plus fields. It never writes log files itself, so the platform owns rotation:

| Platform | Where logs go | Rotation |
|---|---|---|
| systemd | journald | `SystemMaxUse=` / `MaxRetentionSec=` in `/etc/systemd/journald.conf` |
| Docker | the container's log driver | default `json-file`: set `--log-opt max-size=10m --log-opt max-file=5`, or the same `log-opts` in `/etc/docker/daemon.json`; or use `--log-driver journald` |
| Windows (WinSW) | `C:\ProgramData\opencap\logs\opencap.out.log` | `<log mode="roll-by-size">` in the service XML (10 MB × 8 files) |

```bash
journalctl -u opencap -f -o cat                       # follow, JSON only
journalctl -u opencap -o cat | jq 'select(.level == "warning")'
docker logs -f opencap | jq -c 'select(.event == "device.demoted")'
```

The INFO level logs only state changes, plus one `device.stats` line per device every
`stats_interval_s` (default 300 s). Set `log_level: DEBUG` in `poller.yaml` only while
investigating: it logs every request.

## Windows service (WinSW)

opencap runs on Windows as a console process wrapped by
[WinSW v2](https://github.com/winsw/winsw). The service definition is
[`deploy/windows/opencap-service.xml`](../deploy/windows/opencap-service.xml).

1. Install Python 3.12 or 3.13 (64-bit) for all users, then:
   ```bat
   py -3.12 -m venv C:\opencap\venv
   C:\opencap\venv\Scripts\pip install "opencap[modbus,ab,north] @ git+https://github.com/sharmaniraj009/opencap"
   mkdir C:\ProgramData\opencap\config C:\ProgramData\opencap\data C:\ProgramData\opencap\logs
   ```
2. Copy the config files into `C:\ProgramData\opencap\config`.
3. Download the WinSW v2 x64 executable from its GitHub releases. Rename it to
   `opencap-service.exe` and put it in a folder next to `opencap-service.xml` (for example
   `C:\opencap\service\`). WinSW finds its XML by the executable's base name.
4. Secrets: add `<env name="OPENCAP_MQTT_PASSWORD" value="..."/>` (and `OPENCAP_API_TOKEN`)
   to the XML, then restrict the file to administrators and the service account:
   ```bat
   icacls C:\opencap\service\opencap-service.xml /inheritance:r /grant:r Administrators:F SYSTEM:R
   ```
   You can also set them as system environment variables, but every local user can read those.
5. From an elevated prompt:
   ```bat
   C:\opencap\service\opencap-service.exe install
   C:\opencap\service\opencap-service.exe start
   C:\opencap\service\opencap-service.exe status
   ```
   `stop`, `restart` and `uninstall` work the same way.

Notes:

- The service runs `python.exe -m opencap run`, not the `opencap.exe` launcher. That keeps
  one process, so WinSW's Ctrl+C on stop reaches opencap, which then shuts down cleanly.
- Windows has no SIGHUP. Reload with `POST /reload` on the admin API, or restart the service.
- On Windows, `opencap run` uses asyncio's selector event loop, because the MQTT client needs
  `add_reader` (see `docs/decisions.md` D-031).
- By default WinSW runs the service as LocalSystem. To run it as `NT AUTHORITY\LocalService`,
  uncomment `<serviceaccount>` in the XML and grant that account modify rights on
  `C:\ProgramData\opencap\data` and `\logs`.
- CI runs the test suite and this WinSW setup (install, start, `/health`, stop with a clean
  exit 0, uninstall) on windows-latest; see `docs/decisions.md` D-082. It has not been tried on
  plant hardware yet. Please report what you find.

## Rust engine (opencap-engine) as a service

The Rust engine reads the same config directory and behaves the same way (ADR-0002). It is a
single binary, `opencap-engine`, so it needs no Python and no service wrapper. Run it **or**
the Python `opencap` on a machine, not both: they share the config directory and the API port.
`opencap-engine drivers` lists which drivers your build has.

### Linux: systemd (`deploy/opencapd.service`)

The same layout and hardening as `opencap.service` above, with two differences:
`ExecStart=/usr/local/bin/opencap-engine run --config /etc/opencap`, and `Type=notify`. The
engine tells systemd it is ready only once the config has loaded, the poller has started and
the API listens, so `systemctl start opencapd` waits for that and fails when the config does
not load. `systemctl reload opencapd` sends SIGHUP: a diff reload, reported to systemd as
reloading and ready again.

```bash
sudo install -m 0755 opencap-engine /usr/local/bin/opencap-engine
sudo useradd --system --home /var/lib/opencap --shell /usr/sbin/nologin opencap   # if not there yet
sudo cp deploy/opencapd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opencapd
journalctl -u opencapd -f -o cat
```

CI checks the unit with `systemd-analyze verify`, and the readiness, reload and stop
notifications against the real binary (`tests/integration/test_rust_engine_sdnotify.py`).

### Windows: a native service (no WinSW)

From an elevated prompt:

```bat
opencap-engine.exe service install --config C:\ProgramData\opencap\config
sc start opencap-engine
sc query opencap-engine
sc stop opencap-engine
opencap-engine.exe service uninstall
```

- **Install** registers `opencap-engine` to start automatically as LocalSystem, with
  `service run --config <dir>` as its command line (the config path is stored absolute). The
  config defaults to `C:\ProgramData\opencap\config`.
- **Recovery:** restart after 5 s, 5 s, then 30 s; the count resets after a day, and a stop with
  a non-zero exit counts as a failure too.
- **Files:** `C:\ProgramData\opencap\` is the working directory, so a relative `saf.path`
  (`data\saf.sqlite`) lands there. Logs are JSON lines in `logs\opencap-engine.log`. Rotate or
  trim that file yourself; the engine only appends to it.
- **Stop and shutdown** do the same clean stop as SIGTERM on Linux: the API stops, PLC
  connections close, the outbox keeps whatever the broker has not acknowledged, and `offline`
  is published.
- **Event log:** source `opencap-engine` under Application (created at install with
  `eventcreate`), with "started", "stopped" and, if a run fails, the error that ended it.
- **Uninstall** stops the service and removes it and its event log source. The config, the
  outbox and the logs stay.
- There is no reload on Windows yet: change the config, then `sc stop` and `sc start`.

CI installs, starts (against a simulator and the test broker), checks `/health`, stops
(exit code 0, `poller_stopped` in the log, the event log entries) and uninstalls the service
on windows-latest (`deploy/windows/engine_service_smoke.py`). It has not been tried on plant
hardware yet. Please report what you find.

## Commissioning a Siemens S7 or Mitsubishi PLC

The S7 and MC drivers are tested against simulators only (D-058). On the first real PLC of
each kind, before the cut-over:

1. `opencap identity "<channel>/<S7 device>"`: CPU module and order code (SZL), firmware,
   run/stop, and the PDU the CPU negotiated (`pdu_negotiated`). If it is below
   `pdu_requested`, set `params.max_pdu` to it in `overrides.yaml` so blocks are planned to
   fit (D-051). A connect that fails during the ISO handshake may mean the link type: try
   `params.link_type: 1` (PG), D-050.
2. `opencap identity "<channel>/<MELSEC device>"`: CPU model from "read CPU model". It confirms
   that the importer's `plc_type: Q` guess talks to this PLC (D-054). Check `protocol`
   (TCP/UDP) too.
3. `opencap read "<channel>/<device>"`: compare with the KEPServerEX Quick Client, especially
   S7 timers and counters (milliseconds, BCD) and MELSEC 32-bit values (`first_word_low`).

## Upgrades and backups

- **Upgrade:** stop, `pip install --upgrade ...` (or pull and rebuild the image), start.
  Messages queued in `saf.sqlite` are kept and sent after the restart.
- **Back up** the config directory. Of the generated files, keep `devices_map.yaml` and
  `overrides.yaml`: they hold your hand edits, and the importer never overwrites them.
  `devices.json` can be regenerated from the KEPServerEX export.
- **Do not copy `saf.sqlite` while opencap runs** (SQLite WAL). Stop it first, or simply let
  it drain.
