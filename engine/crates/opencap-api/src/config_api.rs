//! The configuration API's read routes (`/api/v1`, #140): channels, devices, tags, the device map,
//! the overrides, the project export and status. Shapes and status codes follow
//! `src/opencap/api/config_api.py` and `ConfigStore`; every answer with a `revision` also sends it
//! as the `ETag`. Writes, imports and the audit log are not here yet: they answer 501.

#![allow(clippy::result_large_err)] // the Err is the response itself, built once per request

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use opencap_core::config::reader::{
    apply_query, CollectionQuery, ConfigReader, ReadError, CHANNEL_SORT, DEVICE_SORT, MAX_LIMIT, TAG_SORT,
};
use serde_json::{json, Value};

pub const NOT_YET: &str = "this route of the configuration API is not implemented in the Rust engine yet (#140)";

type Reader = Option<Arc<ConfigReader>>;
type Params = Query<HashMap<String, String>>;

fn detail(code: StatusCode, text: impl Into<String>) -> Response {
    (code, Json(json!({"detail": text.into()}))).into_response()
}

pub fn not_yet() -> Response {
    detail(StatusCode::NOT_IMPLEMENTED, NOT_YET)
}

/// The resource with its `revision` also as the ETag (Python `with_rev`).
fn with_rev(body: Value) -> Response {
    let etag = body.get("revision").and_then(Value::as_str).filter(|r| !r.is_empty()).map(|r| format!("\"{r}\""));
    let mut resp = Json(body).into_response();
    if let Some(v) = etag.and_then(|e| HeaderValue::from_str(&e).ok()) {
        resp.headers_mut().insert(header::ETAG, v);
    }
    resp
}

/// Python `_config_error`: `detail` is the pydantic-style errors when there are some.
fn read_error(e: ReadError) -> Response {
    let status = StatusCode::from_u16(e.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let body = if e.errors.is_empty() {
        json!({"detail": e.message})
    } else {
        json!({"detail": e.errors, "message": e.message})
    };
    (status, Json(body)).into_response()
}

fn reader(r: &Reader) -> Result<&ConfigReader, Response> {
    r.as_deref()
        .ok_or_else(|| detail(StatusCode::SERVICE_UNAVAILABLE, "the configuration API needs a config directory"))
}

fn bad_param(loc: &str, msg: String, kind: &str) -> Response {
    (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": [{"loc": ["query", loc], "msg": msg, "type": kind}]})))
        .into_response()
}

/// `q`, `sort`, `order`, `limit`, `offset` (API-17, API-18).
fn collection_query(p: &HashMap<String, String>) -> Result<CollectionQuery, Response> {
    let q = p.get("q").filter(|q| !q.is_empty());
    if q.is_some_and(|q| q.chars().count() > 200) {
        return Err(bad_param("q", "String should have at most 200 characters".into(), "string_too_long"));
    }
    let descending = match p.get("order").map(String::as_str) {
        None | Some("asc") => false,
        Some("desc") => true,
        Some(_) => return Err(bad_param("order", "Input should be 'asc' or 'desc'".into(), "literal_error")),
    };
    let limit = match p.get("limit") {
        None => None,
        Some(v) => match v.parse::<usize>() {
            Ok(n) if (1..=MAX_LIMIT).contains(&n) => Some(n),
            Ok(_) => {
                return Err(bad_param("limit", format!("Input should be between 1 and {MAX_LIMIT}"), "value_error"))
            }
            Err(_) => return Err(bad_param("limit", "Input should be a valid integer".into(), "int_parsing")),
        },
    };
    let offset = match p.get("offset") {
        None => 0,
        Some(v) => v
            .parse::<usize>()
            .map_err(|_| bad_param("offset", "Input should be a valid non-negative integer".into(), "int_parsing"))?,
    };
    Ok(CollectionQuery {
        q: q.cloned(),
        sort: p.get("sort").filter(|s| !s.is_empty()).cloned(),
        descending,
        limit,
        offset,
    })
}

fn paged(key: &str, items: Vec<Value>, revision: &Value, q: &CollectionQuery, fields: &[&str]) -> Response {
    match apply_query(items, q, fields) {
        Ok(page) => with_rev(
            json!({key: page.items, "total": page.total, "limit": page.limit, "offset": page.offset, "revision": revision}),
        ),
        Err(e) => read_error(e),
    }
}

async fn list_channels(State(r): State<Reader>, Query(p): Params) -> Response {
    let (rd, q) = match (reader(&r), collection_query(&p)) {
        (Ok(rd), Ok(q)) => (rd, q),
        (Err(e), _) | (_, Err(e)) => return e,
    };
    match rd.list_channels() {
        Ok(d) => {
            paged("channels", d["channels"].as_array().cloned().unwrap_or_default(), &d["revision"], &q, &CHANNEL_SORT)
        }
        Err(e) => read_error(e),
    }
}

async fn get_channel(State(r): State<Reader>, Path(name): Path<String>) -> Response {
    match reader(&r).map(|rd| rd.get_channel(&name)) {
        Ok(Ok(v)) => with_rev(v),
        Ok(Err(e)) => read_error(e),
        Err(e) => e,
    }
}

async fn list_channel_devices(State(r): State<Reader>, Path(name): Path<String>) -> Response {
    let rd = match reader(&r) {
        Ok(rd) => rd,
        Err(e) => return e,
    };
    match rd.get_channel(&name).and_then(|_| rd.list_devices(Some(&name))) {
        Ok(v) => with_rev(v),
        Err(e) => read_error(e),
    }
}

async fn list_devices(State(r): State<Reader>, Query(p): Params) -> Response {
    let (rd, q) = match (reader(&r), collection_query(&p)) {
        (Ok(rd), Ok(q)) => (rd, q),
        (Err(e), _) | (_, Err(e)) => return e,
    };
    match rd.list_devices(p.get("channel").map(String::as_str)) {
        Ok(d) => {
            paged("devices", d["devices"].as_array().cloned().unwrap_or_default(), &d["revision"], &q, &DEVICE_SORT)
        }
        Err(e) => read_error(e),
    }
}

/// `/devices/{id}`, `/devices/{id}/tags`, `/devices/{id}/tags/{tag}` (device ids contain `/`).
/// `None`: not a configuration read (the runtime suffixes are handled before this).
pub fn device_read(r: &Reader, path: &str, p: &HashMap<String, String>) -> Response {
    let rd = match reader(r) {
        Ok(rd) => rd,
        Err(e) => return e,
    };
    if let Some((id, tag)) = path.rsplit_once("/tags/") {
        return match rd.get_tag(id, tag) {
            Ok(v) => with_rev(v),
            Err(e) => read_error(e),
        };
    }
    if let Some(id) = path.strip_suffix("/tags") {
        let q = match collection_query(p) {
            Ok(q) => q,
            Err(e) => return e,
        };
        return match rd.get_device(id) {
            Ok(d) => paged(
                "tags",
                d["device"]["tags"].as_array().cloned().unwrap_or_default(),
                &d["revision"],
                &q,
                &TAG_SORT,
            ),
            Err(e) => read_error(e),
        };
    }
    match rd.get_device(path) {
        Ok(v) => with_rev(v),
        Err(e) => read_error(e),
    }
}

async fn get_map(State(r): State<Reader>) -> Response {
    answer(&r, ConfigReader::get_map)
}

async fn get_overrides(State(r): State<Reader>) -> Response {
    answer(&r, ConfigReader::get_overrides)
}

async fn project(State(r): State<Reader>, Query(p): Params) -> Response {
    let rd = match reader(&r) {
        Ok(rd) => rd,
        Err(e) => return e,
    };
    match rd.export() {
        Ok(v) => {
            let name =
                format!("attachment; filename=\"opencap-config-{}.json\"", v["revision"].as_str().unwrap_or_default());
            let mut resp = with_rev(v);
            if matches!(p.get("download").map(String::as_str), Some("true" | "1")) {
                if let Ok(h) = HeaderValue::from_str(&name) {
                    resp.headers_mut().insert(header::CONTENT_DISPOSITION, h);
                }
            }
            resp
        }
        Err(e) => read_error(e),
    }
}

async fn project_status(State(r): State<Reader>) -> Response {
    answer(&r, ConfigReader::summary)
}

async fn project_revisions(State(r): State<Reader>) -> Response {
    // revisions written through the API come from the audit log, which has no entries until writes exist
    match reader(&r) {
        Ok(rd) => Json(json!({"current": rd.revision(), "revisions": []})).into_response(),
        Err(e) => e,
    }
}

async fn audit(Query(p): Params) -> Response {
    let num = |k: &str, d: usize| p.get(k).and_then(|v| v.parse().ok()).unwrap_or(d);
    Json(json!({"entries": [], "limit": num("limit", 100), "offset": num("offset", 0)})).into_response()
}

async fn map_entry(State(r): State<Reader>, Path(id): Path<String>) -> Response {
    answer(&r, |rd| rd.get_map_entry(&id))
}

async fn channel_override(State(r): State<Reader>, Path(name): Path<String>) -> Response {
    answer(&r, |rd| rd.get_channel_override(&name))
}

async fn device_override(State(r): State<Reader>, Path(id): Path<String>) -> Response {
    answer(&r, |rd| rd.get_device_override(&id))
}

fn answer(r: &Reader, f: impl FnOnce(&ConfigReader) -> Result<Value, ReadError>) -> Response {
    match reader(r) {
        Ok(rd) => f(rd).map_or_else(read_error, with_rev),
        Err(e) => e,
    }
}

async fn writes_not_yet() -> Response {
    not_yet()
}

/// The routes, with the reader as their state. Writes answer 501 on the same paths.
pub fn routes(reader: Reader) -> Router {
    use axum::routing::any;
    Router::new()
        .route("/api/v1/channels", get(list_channels).post(writes_not_yet))
        .route("/api/v1/channels/{channel}", get(get_channel).patch(writes_not_yet).delete(writes_not_yet))
        .route("/api/v1/channels/{channel}/devices", get(list_channel_devices).post(writes_not_yet))
        .route("/api/v1/devices", get(list_devices))
        .route("/api/v1/map", get(get_map))
        .route("/api/v1/map/defaults", any(writes_not_yet))
        .route(
            "/api/v1/map/devices/{*id}",
            get(map_entry).put(writes_not_yet).patch(writes_not_yet).delete(writes_not_yet),
        )
        .route("/api/v1/overrides", get(get_overrides))
        .route("/api/v1/overrides/channels/{channel}", get(channel_override).put(writes_not_yet).delete(writes_not_yet))
        .route(
            "/api/v1/overrides/devices/{*id}",
            get(device_override).put(writes_not_yet).patch(writes_not_yet).delete(writes_not_yet),
        )
        .route("/api/v1/project", get(project))
        .route("/api/v1/project/status", get(project_status))
        .route("/api/v1/project/revisions", get(project_revisions))
        .route("/api/v1/project/import", any(writes_not_yet))
        .route("/api/v1/project/import/{*rest}", any(writes_not_yet))
        .route("/api/v1/audit", get(audit))
        .with_state(reader)
}
