# How to use opencap

This page takes you from an unpacked download to live PLC data on an MQTT broker. It is written for a first-time user.

## 1. What opencap is

opencap is an open-source industrial data collector. It polls PLCs (programmable logic controllers) over their industrial protocols, publishes the values as JSON to an MQTT broker, and keeps them in a local SQLite outbox while the broker is unreachable, so nothing is lost in a short outage. The engine is **read-only**: it has no write path to a PLC.

**Status: early test version.** This is version 0.1.0 (test build 2, Linux x86_64). Nothing has run against real PLCs yet, and secure OPC UA is untested. Use it on a test bench first.

## 2. Quick start with no PLC (simulator)

The simulator produces computed values, so you can check the whole chain before you touch a PLC.

**You need:** a Linux x86_64 machine and an MQTT broker. On Debian or Ubuntu, install mosquitto on the same machine:

```bash
sudo apt install mosquitto mosquitto-clients
```

Mosquitto listens on port 1883, which is the broker address in the quickstart `poller.yaml` files.

**Unpack the quickstart.** Use the build 2 archive, `opencap-linux-x86_64-build2.tar.gz`, which matches this guide:

```bash
tar xzf opencap-linux-x86_64-build2.tar.gz
cd opencap-linux-x86_64
```

The folder holds the `opencap-engine` binary, a `README.txt`, and three ready-made config folders: `config-sim`, `config-modbus` and `config-opcua`. Run every command below from inside this folder, because the output path `data/saf.sqlite` is relative to the current directory.

**Check the build:**

```bash
./opencap-engine check --config config-sim -v
```

This prints the read plan and a cycle-time estimate for each device.

**Run it:**

```bash
./opencap-engine run --config config-sim
```

**In a second terminal, watch the data and check the health endpoint:**

```bash
mosquitto_sub -h 127.0.0.1 -t '#' -v
curl localhost:8080/health/ready
```

You should see messages on topics such as `opencap/water_plant/tank1/data` (the topic layout is explained in [section 4](#4-what-gets-published-to-mqtt)). `/health/ready` returns 200 once every polled device has finished one cycle, and 503 with the devices still waiting until then.

**Open the web UI** at <http://localhost:8080/ui/>. It is partly limited in this version, because the admin API is still being completed.

Stop `run` with Ctrl-C. It shuts down cleanly.

## 3. Connect your first real PLC

### Choose the driver

Each device in `devices.json` has a `driver` name, and each tag has an `address` in the syntax of that driver.

| Protocol | `driver` value | Example address | Notes |
|---|---|---|---|
| Modbus TCP | `modbus_tcp` | `400001` (holding register) | Any Modbus TCP server or gateway. Data types: uint16, int16, uint32, float32 and others. |
| Modbus RTU | `modbus_rtu` | `400001` | Serial line, or RTU frames over a serial-to-TCP gateway. Needs `transport` and serial settings. |
| Allen-Bradley ControlLogix, CompactLogix, Micro800 | `ab_logix` | a symbolic tag name from your PLC project | EtherNet/IP. |
| Allen-Bradley SLC 500, MicroLogix (EtherNet/IP) | `ab_pccc` | `N7:0`, `T4:0.ACC` | PCCC data-table addresses. |
| Allen-Bradley SLC 500, MicroLogix (serial) | `ab_df1` | `N7:0` | DF1 full duplex over a serial line or gateway. |
| Siemens S7-300, 400, 1200, 1500 | `s7` | `MW10` | Rack and slot are set in `params`. |
| Mitsubishi MELSEC Q, L, iQ-R | `melsec_mc` | `D100` | MC protocol 3E frame, TCP or UDP. |
| Omron CS1, CJ1, CJ2 | `omron_fins` | `D100` | FINS over UDP or TCP. |
| OPC UA server | `opcua_client` | `ns=2;s=Name` or `i=2259` | Polled reads of NodeIds. Security policy `None` is the tested path. |
| Simulator | `simulator` | `SINE(60000, 20, 95)` | No network. Used for the quick start. |

Run `./opencap-engine drivers` to list the drivers this build implements. The per-driver parameters and address syntax are on the [driver pages](drivers/index.md).

### Config folder layout

A config folder holds these files (the engine also reads an `overrides.yaml` if you add one, which you can skip for a first setup):

```text
config-modbus/
  poller.yaml        global settings: MQTT broker, outbox, admin API
  devices.json       channels, devices and the tags to read
  devices_map.yaml   the SubDeviceId (and optional topic) for each device
```

### A minimal Modbus TCP example

Copy the quickstart Modbus folder, so the original stays intact, and edit the copy:

```bash
cp -r config-modbus my-plc
```

`my-plc/devices.json` is the file you change. Replace `PLC_IP_HERE` with your PLC's IP address, and set the tags to the registers you want. This is the content of the quickstart file:

```json
{
 "version": 1,
 "channels": [
  {
   "name": "Plant",
   "enabled": true,
   "devices": [
    {
     "id": "Plant/PLC1",
     "channel": "Plant",
     "name": "PLC1",
     "driver": "modbus_tcp",
     "host": "PLC_IP_HERE",
     "port": 502,
     "params": {},
     "scan_ms": 1000,
     "min_scan_ms": 500,
     "timeout_ms": 3000,
     "retries": 2,
     "demote_after": 3,
     "demote_for_ms": 30000,
     "enabled": true,
     "publish": {"mode": "interval", "interval_ms": 2000, "heartbeat_ms": 300000, "deadband": 0.0, "env": "test", "format": "subdevice_json"},
     "tags": [
      {"name": "Reg1", "address": "400001", "dtype": "uint16"},
      {"name": "Reg2", "address": "400002", "dtype": "int16"}
     ]
    }
   ]
  }
 ]
}
```

`my-plc/devices_map.yaml` sets the SubDeviceId that appears in the MQTT payload. Use your own name:

```yaml
defaults:
  env: test
devices:
  Plant/PLC1:
    sub_device_id: PHOENIX_PLC_1
```

`my-plc/poller.yaml` points at the local broker and the outbox. Leave it as it is for a first test:

```yaml
instance: opencap-quickstart
log_level: INFO
mqtt:
  host: 127.0.0.1
  port: 1883
  client_id: opencap-quickstart
saf:
  path: data/saf.sqlite
api:
  bind: 127.0.0.1
  port: 8080
```

**Register numbering.** The quickstart describes `400001` as holding register 1. The `zero_based` parameter of `modbus_tcp` (default `true`, see the [Modbus TCP page](drivers/modbus-tcp/index.md)) decides how these numbers map to the registers your device uses. Compare one read with a value you know from the PLC before you trust the numbers.

### Check, read, run

Run these from the unpacked folder:

```bash
./opencap-engine check --config my-plc -v                      # read plan, no values
./opencap-engine read "Plant/PLC1" --config my-plc             # one read, printed, no MQTT
./opencap-engine run  --config my-plc                          # poll and publish
```

In another terminal, `mosquitto_sub -h 127.0.0.1 -t '#' -v` shows the data as it is published. Device ids are `<channel>/<device>`, so `Plant/PLC1` is the device named `PLC1` in the channel `Plant`.

If your broker needs a login, set `mqtt.username` in `poller.yaml` and put the password in the environment variable `OPENCAP_MQTT_PASSWORD`. Secrets are never written into the config files.

### OPC UA and serial examples

- **OPC UA:** copy `config-opcua` in the same way. Set `PLC_IP_HERE` and the port (4840 is common), and use real NodeIds as tag addresses. The sample tag `i=2259` is the server's own State node and works on any OPC UA server, so it is a good first test.
- **Serial PLCs** (`modbus_rtu`, `ab_df1`) need `transport` and serial settings in `params`. Read the [driver pages](drivers/index.md) for your protocol before you start, and see the limits in section 7.

## 4. What gets published to MQTT

opencap publishes on these topics. `opencap` is the default topic prefix (`mqtt.topic_prefix`). Channel, device and instance names are lowercased, and any character outside `a-z 0-9 . _ -` becomes `_`, so `Plant A` becomes `plant_a`.

| Topic | Retained | Content |
|---|---|---|
| `opencap/<channel>/<device>/data` (or the device's own topic) | no | The values, as SubDevice JSON |
| `opencap/<channel>/<device>/status` | yes | `state` (`ok`, `offline` or `demoted`), `last_good` time, `errors` count |
| `opencap/poller/<instance>/status` | yes | `online` after each connect, `offline` on a clean stop and as the last-will message |

A data message carries only tags with good quality in `Data`. If a tag is not good, `DataValid` is false and the tag is listed in `BadTags`. `DateTime` is the time of the newest good read. A device with no good read since start publishes only status messages. Delivery is QoS 1, so after a reconnect a message can arrive twice.

For the complete topic and quality rules, see the [payload spec](spec/payload-iotgw-json.md).

## 5. Test store-and-forward (broker outage)

This test shows that data survives a broker outage. Run it with a device that is publishing.

1. Start `run` with your config, and keep `mosquitto_sub -h 127.0.0.1 -t '#' -v` running.
2. Stop the broker: `sudo systemctl stop mosquitto`. Wait 30 seconds. While the broker is down, opencap keeps the messages in its outbox, `data/saf.sqlite`.
3. Start the broker again: `sudo systemctl start mosquitto`.
4. The messages from the gap arrive at the subscriber after the reconnect. Duplicates are possible with QoS 1.

## 6. Troubleshooting

Start with these commands, and run them from the folder that holds your config:

```bash
./opencap-engine check --config my-plc -v        # config errors, driver not compiled in, read plan
./opencap-engine read "Plant/PLC1" --config my-plc   # does one read work, and what does it return?
./opencap-engine identity "Plant/PLC1" --config my-plc   # vendor and model, if the PLC reports them
./opencap-engine info                            # support summary
curl localhost:8080/health/ready                 # 503 lists the devices still waiting
curl localhost:8080/health/live                  # the process is up
```

The engine writes logs as JSON lines on stdout.

**When you ask for help,** send:

- the terminal output of `check -v`, of `read`, and the first 30 lines of `run`;
- the output of `curl localhost:8080/api/v1/support`. It hides addresses, so it is safe to paste.

Read the output before you post it anywhere public.

## 7. Limits of this version

- **Early test version (0.1.0).** Linux x86_64 only. Nothing has run against real PLCs yet.
- **Read-only by design.** The engine has no write path to a PLC.
- **OPC UA client:** polled reads only, no subscriptions yet. Security policy `None` is the tested path. Secure policies against a real PLC are untested.
- **OPC UA server** (for SCADA clients) is read-only and off by default.
- **Drivers differ in maturity.** `opencap-engine drivers` lists what your build implements. The engine README records that some drivers were still being ported when it was written, and the project README marks S7, MELSEC and OPC UA as still needing a field check.
- **Serial lines:** in the Rust engine, the loader does not yet apply the `serial_ports` mapping. Give `modbus_rtu` and `ab_df1` devices `transport: tcp` or `transport: serial` directly in their `params`.
- **Web UI and admin API:** partly limited. Admin API parity is still in progress.
- **Health and support endpoints** (`/health/ready`, `/health/live`, `/api/v1/about`, `/api/v1/support`) are open. With no API token configured, the admin API is read-only and answers only requests from localhost.
