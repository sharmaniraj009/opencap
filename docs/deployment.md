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
docker build -t opencap .                                   # drivers: modbus, ab (+ north side)
docker build --build-arg EXTRAS=modbus,ab,north,s7 -t opencap .   # more drivers
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
- **Quick Client (live values)** uses the WebSocket `/api/v1/ws` on the same port. A reverse proxy in front must
  pass the WebSocket upgrade (`Upgrade` and `Connection` headers) and keep the `Host` header, or list the public
  origin in `api.cors_origins`: browsers may open the feed only from the API's own origin (D-066).
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
- Windows support has not been tested on real hardware yet. Please report what you find.

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
