# Running the Rust engine on Linux

A one-page guide for trying `opencap-engine` on a Linux x86_64 machine, for example against a real PLC.
The engine is **read-only** toward PLCs: it never writes a tag.

## Get the binary

Start the **Release** workflow by hand (Actions → Release → Run workflow, *dry run* on). Its run
publishes nothing, and attaches the artifact `engine-linux-x86_64` (a `.tar.gz` with the binary,
`LICENSE`, `NOTICE` and an `example-config/` folder). The same run writes `SHA256SUMS` in its summary.
There is no aarch64 binary yet: use the container image or build on the machine
(`cargo build --release --manifest-path engine/Cargo.toml -p opencap-engine`).

```bash
tar xzf opencap-engine-*-linux-x86_64.tar.gz && cd opencap-engine-*-linux-x86_64
sha256sum opencap-engine          # compare with the checksum of the run
./opencap-engine --version
```

## Config directory

A directory with these files (`example-config/` has the first three; `overrides.yaml` is optional); the engine reads them and never writes them:

| File | Content |
|---|---|
| `poller.yaml` | the MQTT broker, the API, the payload; every key optional |
| `devices.json` | channels (sites) and devices: driver, host, port, tags |
| `devices_map.yaml` | which devices publish, and their topics |
| `overrides.yaml` | per-device overrides |

A project v2 (`project.json` + `node.yaml`) works too.

## Try it, step by step

```bash
./opencap-engine check --config ./config -v            # validate; prints the block plan and cycle time
./opencap-engine read  "Site 1/PLC1" --config ./config # one read of a device, values on stdout, no MQTT
./opencap-engine run   --config ./config               # poll, publish to MQTT, serve the API and web UI
```

`check` and `read` need no broker. Device ids are `<channel>/<device>`.

## MQTT broker

In `poller.yaml`:

```yaml
mqtt:
  host: 192.168.1.10      # the broker
  port: 1883              # 8883 with tls: true
  username: opencap
  password_env: OPENCAP_MQTT_PASSWORD
```

The password comes from the environment, never from the file:
`export OPENCAP_MQTT_PASSWORD=...`. Data goes to `<topic_prefix>/<channel>/<device>/data` (default prefix `opencap`).
While the broker is down, messages wait in `data/saf.sqlite` (relative to the working directory).

## API and web UI

`api.bind` defaults to `127.0.0.1:8080`: the web UI is at `http://127.0.0.1:8080/ui/`, `GET /health` is open.
Set a token with `export OPENCAP_API_TOKEN=$(openssl rand -hex 24)` and send `Authorization: Bearer <token>`.
Binding any other address needs a token, or the engine refuses to start.

## Logs and stopping

One JSON line per event on stderr. `Ctrl-C` (or `SIGTERM`) stops cleanly: the engine publishes `offline` on its
status topic and closes the PLC connections. `SIGHUP` reloads the config.
