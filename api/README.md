# opencap API contract

`openapi.json` is the committed OpenAPI 3.1 contract of the admin + configuration API
(ADR-0002: both engines serve it; ADR-0010: the UI uses only `/api/v1`). It is generated from the
FastAPI app, offline, and checked by `tests/unit/test_openapi_contract.py`.

```
python scripts/gen_openapi.py           # regenerate after changing a route or a model
python scripts/gen_openapi.py --check   # what CI runs (the drift test does the same)
```

The running server does not serve this document (`openapi_url=None`, D-019).

## Compatibility rules (v1)

- **Additive only inside `/api/v1`.** New routes, new optional query parameters, new response fields and new
  enum values are allowed; the minor version in `info.version` grows. Clients must ignore unknown fields.
- **Never, inside v1:** removing or renaming a route, a field or a parameter; making an optional field
  required; changing a type, a status code or the meaning of a field; tightening validation of a request.
  These need `/api/v2` and one release with both.
- **Legacy paths** (`/channels`, `/devices/{id}`, `/devices/{id}/values`, `POST /devices/{id}/read-once`,
  `POST /reload`) are aliases of the `/api/v1` runtime routes, marked `deprecated`; they stay for at least
  one release after the first tagged release and are then removed in the first non-additive change.
- **Open routes** (no token): `/health`, `/metrics`, `/api/v1/auth-info`.
- **Errors** always have the body `{"detail": ...}` (`Error`): a string, or for 422 a list of
  `{loc, msg, type}`. Status codes: 401 (no or wrong token), 403 (role), 404, 409 (conflict), 412 (stale
  `If-Match`), 422 (invalid change, nothing written), 428 (`If-Match` missing), 413, 503 (no config
  directory or read-only config), 400 (`reload failed, old config kept`).
- **Device ids** are `<channel>/<device>` and contain `/` (and often spaces): in a path, send them as is
  (`{device_id}` accepts slashes); URL-encode spaces.
- **Request bodies** of configuration writes are JSON documents; their field-level schema is served by
  `GET /api/v1/project/schema` (not duplicated here, see D-063).
- The Rust engine must pass the same contract; the dual-engine acceptance suite (#42) will run
  `tests/unit/test_openapi_contract.py`-style checks against both.
