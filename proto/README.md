# opencap plugin protocol (`proto/`)

Protocol Buffers contracts between the engine and out-of-process plugins
([ADR-0004](../docs/adr/0004-driver-contract-and-plugin-model.md) §3,
[03-deep-dive](../docs/architecture/03-deep-dive.md) §3.3).

| File | Package | Used by |
|---|---|---|
| `opencap/plugin/v1/driver.proto` | `opencap.plugin.v1` | Rust engine client (`engine/crates/opencap-plugin`, compiled at build time with `protox`, no `protoc` needed); Python driver host (`src/opencap/plugin_host.py`, stubs generated into `src/opencap/plugin/v1/`) |

Regenerate the Python stubs after editing the `.proto` (`tests/unit/test_plugin_proto.py` fails while they are out
of date):

```bash
python -m grpc_tools.protoc -I proto --python_out=src --pyi_out=src --grpc_python_out=src proto/opencap/plugin/v1/driver.proto
```

## Compatibility rules

- `opencap.plugin.v1` only changes **additively**: new fields with new numbers, new enum values, new RPCs.
  A field is never renumbered, retyped or reused; a removed field's number and name are `reserved`.
- Receivers ignore unknown fields. An unknown enum value maps to the documented fallback
  (`QUALITY_UNSPECIFIED` → `BAD_DECODE`, `KIND_UNSPECIFIED` → `OTHER`).
- An RPC the other side does not implement answers `UNIMPLEMENTED`; the engine treats that as the driver error
  `UNSUPPORTED` for that call.
- A breaking change is a new package, `opencap.plugin.v2`, in its own directory. A process announces the protocol it
  speaks in `Hello`; the engine refuses a process whose `Hello.protocol` it does not know.
- Semantics follow the in-process driver contract. A plugin does not retry, back off or cache: the engine does.
  There is no write RPC, and none is added without the write-safety RFC ([ADR-0008](../docs/adr/0008-read-only-by-default.md)).

## Launching a plugin process

The engine starts a plugin with the command from `poller.yaml` (`plugins.python.command` for the Python host)
plus these arguments and environment:

| | Meaning |
|---|---|
| `--socket PATH` | listen on a Unix socket at `PATH` (Linux, macOS; the engine creates `PATH`'s directory with mode 0700) |
| `--listen 127.0.0.1:0` | listen on loopback TCP, any free port (Windows, or `plugins.python.transport: tcp`) |
| `--driver NAME` (repeated) | the drivers this process may serve; others are refused |
| `--exit-on-stdin-eof` | exit when stdin closes: the engine holds the pipe, so a plugin never outlives a crashed engine |
| `OPENCAP_PLUGIN_TOKEN` | 64 hex characters, new for every engine run. Every call must carry `authorization: Bearer <token>` (else `UNAUTHENTICATED`); the Python host also uses it to sign block payloads |

When it is ready, the process writes **one line to stdout**:

```
opencap-plugin-ready {"protocol": "opencap.plugin.v1", "address": "unix:/tmp/opencap-plugin-.../plugin.sock"}
```

(`"address": "127.0.0.1:50123"` for TCP). Everything else, logs included, goes to stderr; the engine copies those
lines into its own log as `plugin.stderr`. The engine connects, calls `Hello`, and from then on supervises the
process: a process that exits or stops answering is killed and started again with backoff, its devices see
`CONNECTION_LOST`, and the process is killed when the engine stops.

## Block payloads

`Plan` returns each wire block as opaque `payload` bytes plus the facts the engine needs (`tags`,
`wire_requests`, `describe`). The engine stores them with its plan and sends a payload back in `Read`. The Python
host encodes a block as a pickle with an HMAC-SHA256 tag keyed by `OPENCAP_PLUGIN_TOKEN`, and it refuses (without
unpickling) any payload whose tag does not verify. So a payload from one engine run stays valid when the host
restarts during that run, and the host deserialises nothing it did not produce itself.
