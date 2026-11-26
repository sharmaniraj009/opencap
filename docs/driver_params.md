# `Device.params` contract per driver

The importer (`config/kep_import.py`) writes these keys; the drivers read them with the
defaults below when a key is absent. Hand edits go in `overrides.yaml` → `params`.

KEPServer keys are shown without the driver prefix where obvious.

## modbus_tcp
| key | type | default | KEPServer source |
|---|---|---|---|
| `unit_id` | int | 0 | `servermain.DEVICE_ID_STRING` `<ip>.N` → N |
| `zero_based` | bool | true | `modbus_ethernet.DEVICE_ZERO_BASED_ADDRESSING` |
| `zero_based_bit` | bool | true | `modbus_ethernet.DEVICE_ZERO_BASED_BIT_ADDRESSING` |
| `modicon_bit_order` | bool | false | `modbus_ethernet.DEVICE_MODICON_BIT_ORDER` |
| `byte_order_big` | bool | true | `modbus_ethernet.DEVICE_MODBUS_BYTE_ORDER` (true = standard big-endian bytes in a register) |
| `first_word_low` | bool | true | `modbus_ethernet.DEVICE_FIRST_WORD_LOW` |
| `first_dword_low` | bool | true | `modbus_ethernet.DEVICE_FIRST_DWORD_LOW` |
| `max_coils` | int | 64 | `modbus_ethernet.DEVICE_OUTPUT_COILS` (bits per FC01 request) |
| `max_discrete` | int | 64 | `modbus_ethernet.DEVICE_INPUT_COILS` (bits per FC02 request) |
| `max_input` | int | 64 | `modbus_ethernet.DEVICE_INTERNAL_REGISTERS` (registers per FC04 request) |
| `max_holding` | int | 64 | `modbus_ethernet.DEVICE_HOLDING_REGISTERS` (registers per FC03 request) |
| `max_gap` | int | 4 | — (unused registers allowed between merged tags) |
| `max_gap_bits` | int \| null | null | — (FC01/02 gap in bits; null = `max_gap` × 16) |
| `nonnormal_float` | "zero" \| "unmodified" | "zero" | channel `servermain.CHANNEL_NON_NORMALIZED_FLOATING_POINT_HANDLING` (0 = zero, 1 = unmodified) |
| `close_on_timeout` | bool | true | `modbus_ethernet.DEVICE_ETHERNET_CLOSE_TCP_SOCKET_ON_TIMEOUT` |

## ab_logix  (Micro800 and ControlLogix/CompactLogix, symbolic tags)
| key | type | default | KEPServer source |
|---|---|---|---|
| `micro800` | bool | false | true when the KEPServer driver is "Allen-Bradley Micro800 Ethernet" |
| `path` | str \| null | null | CIP route after the IP in `DEVICE_ID_STRING`, e.g. `"1,0"` from `<ip>,1,0` |
| `slot` | int \| null | null | last element of `path` (backplane slot), e.g. 0 |
| `max_batch` | int | 40 | — (tags per `plc.read(*tags)` call) |
| `batching` | "auto" \| "on" \| "off" | "auto" | — (Micro800 Multiple Service Packet: auto = probe on connect) |
| `connection_size` | int \| null | null | `controllogix_ethernet.DEVICE_CONNECTION_SIZE_BYTES` (wire estimate; also the Forward Open size: above 511 B a Large Forward Open is tried first) |

## ab_pccc  (SLC / MicroLogix / PLC-5 data-table addresses via pycomm3 `SLCDriver`)
| key | type | default | KEPServer source |
|---|---|---|---|
| `path` | str \| null | null | CIP route after the IP, if any |
| `max_elements` | int | 100 | — (elements per file-block read) |
| `max_gap` | int | 4 | — (unused elements allowed between merged tags) |
| `request_size` | int | 232 | `controllogix_ethernet.DEVICE_REQUEST_SIZE` (max data bytes per read; clamped to 255, the PCCC 1-byte limit) |
| `file_check` | bool | true | — (read the PLC's file directory once and reject blocks for missing/short files before sending) |

## s7
| key | type | default | KEPServer source |
|---|---|---|---|
| `rack` | int | 0 | `siemens_tcpip_ethernet.DEVICE_S7_COMMUNICATIONS_CPU_RACK` (0..7) |
| `slot` | int | 1 | `siemens_tcpip_ethernet.DEVICE_S7_COMMUNICATIONS_CPU_SLOT` (0..31) |
| `max_pdu` | int | 480 | `siemens_tcpip_ethernet.DEVICE_S7_COMMUNICATIONS_MAX_PDU` (240..960; PDU requested at connect and used to plan blocks of at most `max_pdu` − 18 bytes; a smaller negotiated PDU splits reads, D-051) |
| `link_type` | int \| str \| null | null | `siemens_tcpip_ethernet.DEVICE_S7_COMMUNICATIONS_300_400_1200_1500_LINK_TYPE` (TSAP connection type: 1 = PG, 2 = OP, 3 = PC; also `"PG"`/`"OP"`/`"PC"`; null = 3, Kepware's default "PC"; enum values assumed, D-050) |
| `byte_order_big` | bool | true | `siemens_tcpip_ethernet.DEVICE_ADDRESSING_BYTE_ORDER` (false = Kepware "Little Endian": the bytes of every 16/32/64-bit value reversed; not applied to timers, counters, strings) |
| `max_gap` | int | 16 | — (unused bytes allowed between merged tags; timers/counters: `max_gap` / 2 elements) |
| `nonnormal_float` | "zero" \| "unmodified" | "zero" | — (NaN/Inf/denormal REAL/LREAL; Kepware's channel default "Replace with Zero"; the importer writes it for modbus_tcp only) |

## melsec_mc
| key | type | default | KEPServer source |
|---|---|---|---|
| `plc_type` | "Q" \| "L" \| "QnA" \| "iQ-L" \| "iQ-R" | "Q" | `mitsubishi_ethernet.DEVICE_CPU` / `DEVICE_MODEL` (importer writes "Q", a guess; only "iQ-R" changes the frames, D-054) |
| `protocol` | "tcp" \| "udp" | "tcp" | `mitsubishi_ethernet.DEVICE_IP_PROTOCOL` (0 = UDP, 1 = TCP) |
| `first_word_low` | bool | true | `mitsubishi_ethernet.DEVICE_FIRST_WORD_LOW` (word order of every 32/64-bit value) |
| `frame` | "3E" | "3E" | — (MC protocol 3E frame, binary; nothing else is supported) |
| `max_words` | int | 960 | — (words per batch read, 1..960, the MC 3E limit) |
| `max_gap` | int | 16 | — (unused words allowed between merged tags) |
| `nonnormal_float` | "zero" \| "unmodified" | "zero" | — (channel `CHANNEL_NON_NORMALIZED_FLOATING_POINT_HANDLING`; not written by the importer for this driver) |
| `network` | int | 0 | `DEVICE_ID_STRING` `:Nzzz` network number (not imported) |
| `pc` | int | 255 | `DEVICE_ID_STRING` `:XXX` PC number (not imported; 255 = the connected station) |
| `dest_moduleio` | int | 1023 (0x3FF) | `mitsubishi_ethernet.DEVICE_CPU` (0x3FF = own CPU, 0x3E0..0x3E3 = CPU No. 1..4; not imported yet) |
| `dest_modulesta` | int | 0 | — (3E request destination module station) |

## opcua_client
| key | type | default | KEPServer source |
|---|---|---|---|
| `endpoint` | str | `opc.tcp://<host>:<port>` | `opcuaclient.CHANNEL_UA_SERVER_ENDPOINT_URL` (channel level) |
| `security_policy` | str | "None" | `opcuaclient.CHANNEL_UA_SERVER_SECURITY_POLICY` |
| `message_mode` | str | "None" | `opcuaclient.CHANNEL_UA_SERVER_MESSAGE_MODE` |
| `username_env` | str \| null | null | set only when the channel has a username: name of the env var holding it |
| `password_env` | str \| null | null | set only when the channel has a password: name of the env var holding it |
| `publishing_interval_ms` | int \| null | null | `opcuaclient.DEVICE_SUBSCRIPTION_PUBLISHING_INTERVAL_MILLISECONDS` (null = the device's fastest rate group) |
| `sampling_interval_ms` | int \| null | null | `opcuaclient.DEVICE_MONITORED_ITEMS_SAMPLE_INTERVAL_MILLISECONDS` (null = each tag's rate) |
| `certificate` | str \| null | null | — (client certificate path, DER or PEM; needed by a security policy other than None) |
| `private_key` | str \| null | null | — (its key; a self-signed pair is generated at both paths when both files are missing) |
| `private_key_password_env` | str \| null | null | — (name of the env var holding the key password, if the key is encrypted) |
| `server_certificate` | str \| null | null | — (pin the server certificate; null = take it from GetEndpoints) |
| `application_uri` | str | "urn:opencap:client" | — (must match the client certificate's URI) |
| `max_items` | int | 100 | — (monitored items per CreateMonitoredItems, nodes per Read, tags per block) |
| `session_timeout_ms` | int | 60000 | — (requested session timeout; a dropped session is cleaned up on the server after it) |

Credentials are **never** copied from the KEPServer export into any generated file.

opcua_client addresses are NodeIds (`ns=7;s=LTI002_PV`, `i=2258`, `ns=4;g=<guid>`,
`ns=5;b=<base64>`, `nsu=<namespace uri>;s=...`). Params named `username`, `password` (or
`user`, `passwd`, `private_key_password`) are rejected: put secrets in environment variables
and name them in the `*_env` params. Security policy names: `None`, `Basic256Sha256`,
`Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss` (case and underscores do not matter); message
mode `None`, `Sign` or `SignAndEncrypt`. See docs/decisions.md D-045.

## Serial lines: `transport` and the COM port mapping  (modbus_rtu, ab_df1)
A serial PLC is reached through `params.transport` (`drivers/_serial.py`):

| `transport` | reached at | notes |
|---|---|---|
| `tcp` (default) | `host:port` | raw TCP to a serial-over-TCP gateway / virtual COM server (TCP server mode): the socket carries the serial bytes, the gateway owns baud and parity |
| `serial` | `host` = device path (`/dev/ttyUSB0`, `COM3`), `port` 0 | local port via pyserial (`pip install 'opencap[serial]'`), opened exclusively with `baudrate`/`bytesize`/`parity`/`stopbits` |
| `rfc2217` | `host:port` | Telnet COM-port-control server (RFC 2217); pyserial negotiates the line settings. **ab_df1 only** |
| `unmapped` | — | what `opencap import-kep` writes for a KEPServer COM port: the device is **disabled with a warning** until the port is mapped |

Common serial params: `baudrate` (19200), `bytesize` (8), `parity` (`N`/`E`/`O`/`M`/`S`, or none/even/odd), `stopbits`
(1, 1.5, 2), `com_port` (the KEPServer COM port, e.g. `COM112`, used by the mapping below).

**Mapping a KEPServer COM port** (`overrides.yaml`). On the KEPServer PC the DF1 PLCs sit behind virtual COM ports
that the virtual-COM software maps to router serial-over-TCP gateways: look up each COM port's gateway address and
TCP port there (or in the router), then:
```yaml
serial_ports:
  COM112: 192.0.2.50:4001            # raw TCP gateway (same as tcp://192.0.2.50:4001)
  COM7: rfc2217://192.0.2.51:4002    # RFC 2217 COM-port server
  COM3: /dev/ttyUSB0                 # local serial port on the opencap host
```
One entry serves every device on that COM port (stations / unit ids on one line share one connection). Keys are
case-insensitive. A device's own override wins: `devices: {"SITE/PLC": {host: 192.0.2.50, port: 4001, params:
{transport: tcp}}}`. A mapping that cannot be parsed, or a transport the driver does not support, makes the config
invalid; an unused `serial_ports` entry is a warning. Check with `opencap check --site SITE` (the device is no longer
`(disabled)`) and `opencap read "SITE/PLC"` (on failure it prints why).

## modbus_rtu  (Modbus RTU on a serial line, or RTU frames over TCP)
Every `modbus_tcp` key above applies unchanged (`unit_id`, `zero_based`, word/byte order, `max_holding`, ...): same
parser, planner and decoder. Plus the serial keys above and:

| key | type | default | meaning |
|---|---|---|---|
| `transport` | `tcp` \| `serial` \| `unmapped` | `tcp` | `tcp` = RTU frames on a TCP socket (gateway in transparent mode); RFC 2217 is not supported |
| `inter_frame_ms` | number \| null | null | silence kept between frames on the line; null = 3.5 characters at `baudrate` (≥ 1.75 ms) |
| `local_echo` | bool | false | drop the echo of our own request (2-wire RS-485 adapters that echo) |
| `close_on_timeout` | bool | true | close the line after a timeout: RTU has no transaction id, so a late reply could otherwise be read as the next one's |

The importer does not create modbus_rtu devices (the migrated project has none); configure them by hand.

**Rust engine** (`engine/drivers/modbus_rtu`, D-070): same keys and messages. Its serial library cannot open a
port with mark/space parity or 1.5 stop bits (the connect error says so), and its loader does not apply
`serial_ports` yet: give Rust-engine RTU devices `transport: tcp` or `serial` directly.

## ab_df1  (Allen-Bradley DF1 full duplex: SLC 500 / MicroLogix data tables over a serial line)
Addresses, block planning and decoding are `ab_pccc`'s, so `max_elements` (100), `max_gap` (4) and `request_size`
(232, ≤ 255) mean the same. Plus the serial keys above and:

| key | type | default | KEPServer source |
|---|---|---|---|
| `station` | int 0–254 | 1 | `servermain.DEVICE_ID_STRING` (read as decimal; see D-037) — the DST station |
| `src_station` | int 0–254 | 0 | — (our station number on the link) |
| `error_check` | `crc` \| `bcc` | `crc` | the DF1 driver's error-checking property (any `*df1.*ERROR_CHECK*`/`CHECKSUM` key; default CRC, flagged) |
| `transport`, `com_port` | | `unmapped`, `COM<n>` | `servermain.CHANNEL_SERIAL_COMMUNICATIONS_COM_ID` |
| `baudrate`, `bytesize`, `parity`, `stopbits` | | 19200 8N1 | `servermain.CHANNEL_SERIAL_COMMUNICATIONS_BAUD_RATE` / `_DATA_BITS` / `_PARITY` (ASCII code: 78 N, 69 E, 79 O) / `_STOP_BITS` |
| `ack_timeout_ms` | int | 1000 | — (wait for DLE ACK/NAK after a frame; then DLE ENQ) |
| `nak_retries` | int | 3 | — (frame re-sent after a NAK) |
| `enq_retries` | int | 3 | — (ENQs after an ACK timeout) |
| `read_fnc` | `a2` \| `a1` | `a2` | — (protected typed logical read with 3 / 2 address fields) |
| `duplicate_detect` | bool | true | — (drop a frame whose SRC/CMD/STS/TNS repeat the previous one) |
| `close_on_timeout` | bool | false | — (replies are matched by TNS, so a late reply is harmless; demotion still closes the line) |

The whole transaction (frame, NAK/ENQ recoveries, reply) must fit in `device.timeout_ms`.

**Supported:** DF1 full duplex (point to point, or several stations behind a bridge on one line), BCC and CRC,
DLE stuffing, ACK/NAK/ENQ with retries, embedded response symbols, duplicate detection, typed reads of N, F, L, B
(incl. `B3/n` bit numbers), I/O (`I:x.y`, `I:x/b`), S, ST, T/C sub-elements, the processor catalog number
(`opencap identity`). **Not supported:** half-duplex master/slave (the importer flags it), PLC-5 typed reads with
system addresses, DH+/DH-485 routing beyond DST/SRC, the file-directory check `ab_pccc` does (a missing file is
rejected by the PLC's STS 0x10 instead), unsolicited messages from the PLC (ACKed and dropped), and any write.
