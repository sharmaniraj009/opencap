# Running the Rust engine on Linux

A one-page guide for trying `opencap-engine` on a Linux x86_64 machine, for example against a real PLC.
The engine is **read-only** toward PLCs: it has no write path.

## Get the binary

Start the **Release** workflow by hand (Actions → Release → Run workflow, *dry run* on). The run publishes
nothing and attaches the artifact `engine-linux-x86_64`: a `.tar.gz` with the binary, `LICENSE`, `NOTICE` and
`example-config/` (two simulated devices). Its summary lists the SHA-256 sums. There is no aarch64 binary yet:
use the container image, or build on the machine
(`cargo build --release --manifest-path engine/Cargo.toml -p opencap-engine`).

```bash
tar xzf opencap-engine-*-linux-x86_64.tar.gz && cd opencap-engine-*-linux-x86_64
sha256sum opencap-engine          # compare with the run's summary
./opencap-engine --version
./opencap-engine drivers          # JSON: which drivers this build implements
```

You also need an MQTT broker. For a quick test: `sudo apt install mosquitto mosquitto-clients` (listens on 1883).

## Step 0: no PLC needed

```bash
./opencap-engine check --config example-config -v        # validate; prints the block plan and cycle time
./opencap-engine run   --config example-config           # poll, publish to MQTT, serve the API and web UI
mosquitto_sub -h 127.0.0.1 -t '#' -v                     # in another terminal: the data
curl localhost:8080/health/ready                         # 200 once every device has finished a cycle
```

Stop with Ctrl-C (clean stop: `offline` on the status topic, PLC connections closed). `SIGHUP` reloads the config.
The web UI is at `http://127.0.0.1:8080/ui/`.

## The config directory

A directory the engine reads and never writes: `poller.yaml` (MQTT, API, payload; every key optional),
`devices.json` (channels, devices, tags), `devices_map.yaml` (publish names and topics) and optionally
`overrides.yaml`. A project v2 (`project.json` + `node.yaml`) works too. Copy `example-config/` and edit it.
Device ids are `<channel>/<device>`.

## Your PLC over Modbus TCP

In a copy of `example-config`, replace the devices in `devices.json` with one like this, and give the same id a
`sub_device_id` in `devices_map.yaml`:

```json
{"id": "Plant/PLC1", "channel": "Plant", "name": "PLC1", "driver": "modbus_tcp",
 "host": "192.168.1.20", "port": 502, "params": {},
 "scan_ms": 1000, "min_scan_ms": 500, "timeout_ms": 3000, "retries": 2,
 "demote_after": 3, "demote_for_ms": 30000, "enabled": true,
 "publish": {"mode": "interval", "interval_ms": 2000, "heartbeat_ms": 300000, "env": "test", "format": "subdevice_json"},
 "tags": [{"name": "Reg1", "address": "400001", "dtype": "uint16"},
          {"name": "Reg2", "address": "400002", "dtype": "int16"}]}
```

Address `400001` is holding register 1; `dtype` is `uint16`, `int16`, `uint32`, `float32` and so on.

```bash
./opencap-engine check --config mycfg -v                 # the read plan
./opencap-engine read "Plant/PLC1" --config mycfg        # one read, values on stdout, no MQTT
./opencap-engine run --config mycfg                      # poll and publish
```

## Your PLC over OPC UA

Same, with `"driver": "opcua_client"`, `"port": 4840`,
`"params": {"security_policy": "None", "message_mode": "None"}` and OPC UA node ids as tag addresses
(`ns=2;s=Name` or `i=1234`). The tag `{"name": "ServerState", "address": "i=2259", "dtype": "int32"}` is the
server's own state and exists on any OPC UA server. The client does polled reads only (no subscriptions yet);
policy `None` is the tested path, secure policies against a real PLC are untested.

## MQTT broker

```yaml
# poller.yaml
mqtt:
  host: 192.168.1.10      # the broker
  port: 1883              # 8883 with tls: true
  username: opencap
  password_env: OPENCAP_MQTT_PASSWORD
```

The password comes from the environment (`export OPENCAP_MQTT_PASSWORD=...`), never from the file. Data goes to
`<topic_prefix>/<channel>/<device>/data` (default prefix `opencap`).

## Outage test (store-and-forward)

With `run` active: stop the broker (`sudo systemctl stop mosquitto`), wait 30 seconds, start it again. The data
from the gap arrives afterwards. The outbox is `data/saf.sqlite`, relative to the working directory.

## API token

`api.bind` is `127.0.0.1:8080` by default. For another address set a token and send it as
`Authorization: Bearer <token>`: `export OPENCAP_API_TOKEN=$(openssl rand -hex 24)`. The engine refuses to bind a
non-loopback address without one.

## When something fails

Send the terminal output of `check`, `read` and the first 30 lines of `run`, plus
`curl localhost:8080/api/v1/support` (safe to paste: it hides addresses).
