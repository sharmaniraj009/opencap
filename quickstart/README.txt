opencap quick start (Linux x86_64, test build 0.1.0, build 2, built from main d56ab9c)

WHAT THIS IS
The Rust opencap engine. It polls PLCs read-only and publishes JSON to an MQTT broker.
Drivers in this build: modbus_tcp, modbus_rtu, ab_logix, ab_pccc, ab_df1, s7, melsec_mc, omron_fins, opcua_client, simulator.
OPC UA client: polled reads only (no subscriptions yet), security policy None is the tested path; secure policies
against a real PLC are UNTESTED. The engine also has an OPC UA SERVER (read-only), off by default.

NEEDS: an MQTT broker (for a quick test: `sudo apt install mosquitto mosquitto-clients`, it listens on 1883).

STEP 0, no PLC needed (checks the build works on your machine)
  ./opencap-engine check --config config-sim -v
  ./opencap-engine run   --config config-sim
  in another terminal:  mosquitto_sub -h 127.0.0.1 -t '#' -v
  and:                  curl localhost:8080/health/ready
  Stop with Ctrl-C.

STEP 1, your PLC over Modbus TCP
  1. Edit config-modbus/devices.json: replace PLC_IP_HERE with the PLC address; set the tags
     (address 400001 = holding register 1, dtype uint16 | int16 | uint32 | float32 ...).
  2. ./opencap-engine check --config config-modbus -v          (shows the read plan)
  3. ./opencap-engine read "Plant/PLC1" --config config-modbus (one read, no MQTT)
  4. ./opencap-engine run  --config config-modbus              (polls and publishes)
  5. mosquitto_sub -h 127.0.0.1 -t '#' -v                      (see the data)

STEP 1b, your PLC over OPC UA (use this if Modbus is not available on the PLC)
  Same as Step 1 but with config-opcua: set PLC_IP_HERE and port (4840 usually), and put real NodeIds as tag addresses
  (ns=2;s=Name or i=1234). The sample tag i=2259 is the server's own State and works on any OPC UA server.
  Web UI (limited, admin API parity is still in progress): http://localhost:8080/ui/

OUTAGE TEST (store-and-forward)
  With `run` active: stop the broker (sudo systemctl stop mosquitto), wait 30 s, start it again.
  The data from the gap should arrive afterwards (the outbox is data/saf.sqlite).

READ-ONLY: the engine has no write path to a PLC.

WHEN SOMETHING FAILS send back: the terminal output of `check`, `read` and the first 30 lines of `run`,
and `curl localhost:8080/api/v1/support` (safe to paste: it hides addresses).
