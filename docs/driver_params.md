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
| `connection_size` | int \| null | null | `controllogix_ethernet.DEVICE_CONNECTION_SIZE_BYTES` (used for the wire estimate now; applied to pycomm3 in Phase 2) |

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
| `max_pdu` | int | 480 | `siemens_tcpip_ethernet.DEVICE_S7_COMMUNICATIONS_MAX_PDU` (240..960; PDU requested at connect and used to plan blocks of at most `max_pdu` − 18 bytes; a smaller negotiated PDU splits reads, D-031) |
| `link_type` | int \| str \| null | null | `siemens_tcpip_ethernet.DEVICE_S7_COMMUNICATIONS_300_400_1200_1500_LINK_TYPE` (TSAP connection type: 1 = PG, 2 = OP, 3 = PC; also `"PG"`/`"OP"`/`"PC"`; null = 3, Kepware's default "PC"; enum values assumed, D-030) |
| `byte_order_big` | bool | true | `siemens_tcpip_ethernet.DEVICE_ADDRESSING_BYTE_ORDER` (false = Kepware "Little Endian": the bytes of every 16/32/64-bit value reversed; not applied to timers, counters, strings) |
| `max_gap` | int | 16 | — (unused bytes allowed between merged tags; timers/counters: `max_gap` / 2 elements) |
| `nonnormal_float` | "zero" \| "unmodified" | "zero" | — (NaN/Inf/denormal REAL/LREAL; Kepware's channel default "Replace with Zero"; the importer writes it for modbus_tcp only) |

## melsec_mc
| key | type | default | KEPServer source |
|---|---|---|---|
| `plc_type` | "Q" \| "L" \| "QnA" \| "iQ-L" \| "iQ-R" | "Q" | `mitsubishi_ethernet.DEVICE_CPU` / `DEVICE_MODEL` (importer writes "Q", a guess; only "iQ-R" changes the frames, D-034) |
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
| `endpoint` | str | — | `opcuaclient.CHANNEL_UA_SERVER_ENDPOINT_URL` (channel level) |
| `security_policy` | str | "None" | `opcuaclient.CHANNEL_UA_SERVER_SECURITY_POLICY` |
| `message_mode` | str | "None" | `opcuaclient.CHANNEL_UA_SERVER_MESSAGE_MODE` |
| `username_env` | str \| null | null | set only when the channel has a username: name of the env var holding it |
| `password_env` | str \| null | null | set only when the channel has a password: name of the env var holding it |
| `publishing_interval_ms` | int \| null | null | `opcuaclient.DEVICE_SUBSCRIPTION_PUBLISHING_INTERVAL_MILLISECONDS` |
| `sampling_interval_ms` | int \| null | null | `opcuaclient.DEVICE_MONITORED_ITEMS_SAMPLE_INTERVAL_MILLISECONDS` |

Credentials are **never** copied from the KEPServer export into any generated file.
