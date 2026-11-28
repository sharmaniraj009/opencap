//! Admin REST API: the same routes, auth and status codes as `src/opencap/api/server.py`.
//! Bind to localhost or the VPN only.
//!
//! - `GET /health`, `GET /metrics`: open (monitoring).
//! - `GET /channels`, `GET /devices/{id}`, `GET /devices/{id}/values`,
//!   `POST /devices/{id}/read-once`, `POST /reload`: `Authorization: Bearer <token>`.
//! - Tokens (`src/opencap/api/auth.py`, D-019, D-040): `api.token_env` is the legacy token
//!   "default" with role admin; `api.tokens` adds named tokens with role `viewer` (GET/HEAD
//!   only) or `admin` (also `POST`: reload, read-once). Secrets come from env vars.
//! - With no token at all the API refuses to start unless bound to a loopback address, and
//!   is read-only: every caller is the viewer "anonymous", and only requests addressed to a
//!   loopback host name are answered (DNS rebinding).
//! - Device ids contain `/` (`<channel>/<device>`), so the `/values` and `/read-once`
//!   suffixes are matched first, as in the Python routes.
//!
//! The engine implements [`EngineView`]; handlers never wait on PLC I/O except
//! `read-once`, which is the one route that is supposed to.

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use axum::body::{Body, HttpBody};
use axum::extract::{ConnectInfo, Path, Query, Request, State};
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use http_body_util::BodyExt;
use serde_json::{json, Value};

pub mod config_api;
pub mod events;
pub mod ui;
pub use events::{Refusal, RefusalLog};

#[derive(Debug)]
pub enum ReadOnceError {
    UnknownDevice,
    Failed(String),
}

#[async_trait]
pub trait EngineView: Send + Sync + 'static {
    fn health(&self) -> Value;
    fn channels_summary(&self) -> Value;
    /// None = unknown device (404).
    fn device_detail(&self, device_id: &str) -> Option<Value>;
    fn device_values(&self, device_id: &str) -> Option<Value>;
    /// The device's `_System.*` and `_Statistics.*` tags keyed by device-relative path
    /// (`values?include_system=true`, spec section 10). `None` = no system tags.
    fn device_system_values(&self, _device_id: &str) -> Option<Value> {
        None
    }
    /// `GET /api/v1/system/tags[?prefix=]`: `{"items": [{path, dtype, value, quality, source_ts}], "revision"}`.
    fn system_tags(&self, _prefix: &str) -> Value {
        serde_json::json!({"items": [], "revision": ""})
    }
    async fn read_once(&self, device_id: &str) -> Result<Value, ReadOnceError>;
    /// Err = the new config was rejected; the old one is kept.
    async fn reload(&self) -> Result<Value, String>;
    /// A reload asked for through the API: `who` is the token name and `via` `api` or `ui`, for the
    /// `config_reloaded` line. Engines that do not log them can leave the default.
    async fn reload_by(&self, _who: &str, _via: &str) -> Result<Value, String> {
        self.reload().await
    }
    /// Prometheus text exposition.
    fn metrics(&self) -> String;
    /// `/health/ready` (D-075): `{"ready", "waiting": [device ids], "reason"?}`.
    fn readiness(&self) -> Value;
    /// `/api/v1/about`: build facts only, open.
    fn about(&self) -> Value;
    /// `/api/v1/support`: the support summary (viewer), as `opencap-engine info`.
    fn support(&self) -> Value;
    /// `GET /api/v1/project/pending` and `status.config`: does the config on disk differ from the
    /// running one? `{"available", "pending_reload", "revision"}`.
    fn config_status(&self) -> Value {
        json!({"available": false, "pending_reload": false})
    }
    /// `GET /api/v1/devices/{id}/plan`: the block plan of the running config; `None` = not polled.
    fn device_plan(&self, _device_id: &str) -> Option<Value> {
        None
    }
    /// `GET /api/v1/devices/{id}/identity`: `{"connected", "identity"}` over the device's
    /// open connection (never opens one). `Err(UnknownDevice)` = not polled.
    async fn device_identity(&self, _device_id: &str) -> Result<Value, ReadOnceError> {
        Err(ReadOnceError::UnknownDevice)
    }
}

pub const METRICS_CONTENT_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";

/// A loopback bind address (Python `server._is_loopback`): `localhost` or any loopback IP,
/// all of 127.0.0.0/8 included.
pub fn is_loopback(host: &str) -> bool {
    host == "localhost" || host.parse::<IpAddr>().map(|ip| ip.is_loopback()).unwrap_or(false)
}

/// The `Host` names open mode answers (Python `auth.LOOPBACK_HOSTS`): exactly these, so the
/// check is the same in both engines (the bind check above is wider, as in Python).
pub const LOOPBACK_HOSTS: [&str; 3] = ["127.0.0.1", "localhost", "::1"];

/// Python `create_app()`'s start-up rule.
pub fn check_bind(bind: &str, auth: &Auth, token_env: &str) -> Result<(), String> {
    if auth.is_open() && !is_loopback(bind) {
        return Err(format!("API bound to {bind} without a token: set {token_env} or bind to 127.0.0.1"));
    }
    Ok(())
}

/// Read the token from the env var (empty = unset, as Python's `os.environ.get(...) or None`).
pub fn token_from_env(token_env: &str) -> Option<String> {
    std::env::var(token_env).ok().filter(|t| !t.is_empty())
}

/// What a token may do (Python `Role`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    /// Safe methods only (GET/HEAD).
    Viewer,
    /// Also reload and read-once.
    Admin,
}

/// One entry of `api.tokens`: the secret is read from the env var `token_env`.
#[derive(Debug, Clone)]
pub struct TokenSpec {
    pub name: String,
    pub role: Role,
    pub token_env: String,
}

/// The legacy `api.token_env` token's name.
pub const LEGACY_NAME: &str = "default";
const OPEN_READ_ONLY: &str = "no API token is configured, so the API is read-only: \
                              set api.tokens (or api.token_env) in poller.yaml to make changes";

/// Who is calling (Python `Principal`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Principal {
    pub name: String,
    pub role: Role,
}

/// The API's tokens (Python `Auth`): built once at start-up from the env.
#[derive(Debug, Clone, Default)]
pub struct Auth {
    tokens: Vec<(Vec<u8>, String, Role)>,
}

impl Auth {
    /// `legacy` is the `api.token_env` secret (if set). A listed token whose env var is unset,
    /// or two tokens with the same secret, stop the start (same messages as Python).
    pub fn new(
        legacy: Option<String>,
        specs: &[TokenSpec],
        env: impl Fn(&str) -> Option<String>,
    ) -> Result<Self, String> {
        let mut tokens = Vec::new();
        if let Some(secret) = legacy.filter(|t| !t.is_empty()) {
            tokens.push((secret.into_bytes(), LEGACY_NAME.to_string(), Role::Admin));
        }
        for (i, spec) in specs.iter().enumerate() {
            let Some(secret) = env(&spec.token_env).filter(|t| !t.is_empty()) else {
                return Err(format!("api.tokens[{i}] {}: env var {} is not set", py_repr(&spec.name), spec.token_env));
            };
            tokens.push((secret.into_bytes(), spec.name.clone(), spec.role));
        }
        let mut secrets: Vec<&[u8]> = tokens.iter().map(|t| t.0.as_slice()).collect();
        secrets.sort();
        if secrets.windows(2).any(|w| w[0] == w[1]) {
            return Err("api tokens: two tokens have the same secret; give each its own".into());
        }
        Ok(Self { tokens })
    }

    /// One admin token (tests, tools).
    pub fn single(token: &str) -> Self {
        Self { tokens: vec![(token.as_bytes().to_vec(), LEGACY_NAME.to_string(), Role::Admin)] }
    }

    /// How many tokens are configured (the legacy one included).
    pub fn token_count(&self) -> usize {
        self.tokens.len()
    }

    /// No token configured: read-only, loopback only.
    pub fn is_open(&self) -> bool {
        self.tokens.is_empty()
    }

    /// The token's name and role. Compares against every token (constant time).
    fn check_token(&self, given: &str) -> Option<(String, Role)> {
        let mut found = None;
        for (secret, name, role) in &self.tokens {
            if ct_eq(given.as_bytes(), secret) && found.is_none() {
                found = Some((name.clone(), *role));
            }
        }
        found
    }

    /// Authorize a request (Python `Auth.principal`): Err is the 401/403 response and whom the
    /// log names for it.
    #[allow(clippy::result_large_err)] // the Err is the response itself, built once per request
    pub fn principal(&self, method: &Method, headers: &HeaderMap) -> Result<Principal, Refused> {
        let safe = matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS);
        if self.is_open() {
            let host = headers.get(header::HOST).and_then(|v| v.to_str().ok()).unwrap_or("");
            if !LOOPBACK_HOSTS.contains(&host_name(host).as_str()) {
                return Err(Refused::new(
                    detail(
                        StatusCode::FORBIDDEN,
                        "no API token is configured: only requests to localhost are answered",
                    ),
                    Some("anonymous"),
                ));
            }
            if !safe {
                return Err(Refused::new(detail(StatusCode::FORBIDDEN, OPEN_READ_ONLY), Some("anonymous")));
            }
            return Ok(Principal { name: "anonymous".into(), role: Role::Viewer });
        }
        let value = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).unwrap_or("");
        let (scheme, given) = value.split_once(' ').unwrap_or((value, ""));
        let given = given.trim();
        let found =
            if scheme.eq_ignore_ascii_case("bearer") && !given.is_empty() { self.check_token(given) } else { None };
        let Some((name, role)) = found else {
            let mut resp = detail(StatusCode::UNAUTHORIZED, "missing or bad bearer token");
            resp.headers_mut().insert(header::WWW_AUTHENTICATE, header::HeaderValue::from_static("Bearer"));
            return Err(Refused::new(resp, None));
        };
        if !safe && role != Role::Admin {
            return Err(Refused::new(
                detail(StatusCode::FORBIDDEN, format!("token {} has role viewer: read-only", py_repr(&name))),
                Some(&name),
            ));
        }
        Ok(Principal { name, role })
    }
}

/// A refused request: the response, and the token name the log gives for it (`None` = unknown).
pub struct Refused {
    pub response: Response,
    pub who: Option<String>,
}

impl Refused {
    fn new(response: Response, who: Option<&str>) -> Self {
        Self { response, who: who.map(str::to_string) }
    }
}

/// The name part of a Host header: `[::1]:8080` -> `::1`, `localhost:8080` -> `localhost`.
pub fn host_name(host: &str) -> String {
    let host = host.trim().to_ascii_lowercase();
    if let Some(rest) = host.strip_prefix('[') {
        return rest.split(']').next().unwrap_or("").to_string();
    }
    if host.matches(':').count() == 1 {
        host.rsplit_once(':').map(|(h, _)| h.to_string()).unwrap_or(host)
    } else {
        host
    }
}

struct AppState {
    view: Arc<dyn EngineView>,
    auth: Auth,
    refusals: RefusalLog,
    config: Option<Arc<opencap_core::config::reader::ConfigReader>>,
    /// `device id -> (when, answer)` of connected identity answers: asking can cost a request
    /// on the wire, so it is asked at most once per [`IDENTITY_TTL`] (Python `IDENTITY_TTL_S`).
    identity_cache: std::sync::Mutex<HashMap<String, (std::time::Instant, Value)>>,
}

/// How long a connected identity answer is served from the cache.
pub const IDENTITY_TTL: Duration = Duration::from_secs(300);

type St = State<Arc<AppState>>;

fn detail(code: StatusCode, text: impl Into<String>) -> Response {
    (code, Json(json!({ "detail": text.into() }))).into_response()
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Paths answered without a token (Python `guard.OPEN_PATHS` / `OPEN_PREFIXES`): the health
/// and metrics routes, `/api/v1/about`, `/api/v1/auth-info` and the UI bundle.
const OPEN_PATHS: [&str; 8] =
    ["/health", "/health/ready", "/health/live", "/metrics", "/api/v1/auth-info", "/api/v1/about", "/", "/ui"];
const OPEN_PREFIX: &str = "/ui/";
/// Request body cap (Python `guard.MAX_BODY`).
pub const MAX_BODY: usize = 1024 * 1024;
/// How much of a refused body is still read and dropped so the client gets the 413 (see
/// [`drain`]); a body declared larger is refused without reading it.
pub const DRAIN_MAX: u64 = 8 * 1024 * 1024;
/// How long a refused body is drained at most.
pub const DRAIN_TIME: Duration = Duration::from_secs(5);

pub fn is_open_path(path: &str) -> bool {
    OPEN_PATHS.contains(&path) || path.starts_with(OPEN_PREFIX)
}

/// Python `ApiGuard`: authenticate from the headers before a single body byte is read, then
/// cap the body: a `Content-Length` over the cap, or a chunked body passing it, gets 413 and
/// the rest is drained, never kept (see [`drain`]). The open paths and `OPTIONS` (no token)
/// take no body at all: any body is refused at once, unread. Runs in front of every route and
/// the fallback.
async fn guard(State(st): St, req: Request, next: Next) -> Response {
    let (parts, mut body) = req.into_parts();
    let client = parts
        .extensions
        .get::<ConnectInfo<SocketAddr>>()
        .map_or_else(|| events::UNKNOWN.to_string(), |c| c.0.ip().to_string());
    let via =
        if parts.headers.get("x-opencap-client").and_then(|v| v.to_str().ok()) == Some("ui") { "ui" } else { "api" };
    let refuse = |status: StatusCode, who: Option<&str>| {
        st.refusals.refused(parts.method.as_str(), parts.uri.path(), status.as_u16(), who, &client, via);
    };
    let declared = parts
        .headers
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse::<u64>().ok());
    if is_open_path(parts.uri.path()) || parts.method == Method::OPTIONS {
        // no token here, so no body is read either: an anonymous caller cannot make the
        // server buffer or drain anything
        let has_body = declared.is_some_and(|n| n > 0)
            || parts.headers.contains_key(header::TRANSFER_ENCODING)
            || (!body.is_end_stream() && body.size_hint().exact() != Some(0));
        if has_body {
            refuse(StatusCode::PAYLOAD_TOO_LARGE, None);
            return closing(detail(StatusCode::PAYLOAD_TOO_LARGE, "this request takes no body"));
        }
        return next.run(Request::from_parts(parts, Body::empty())).await;
    }
    let principal = match st.auth.principal(&parts.method, &parts.headers) {
        Ok(p) => p,
        Err(refused) => {
            refuse(refused.response.status(), refused.who.as_deref());
            return refused.response;
        }
    };
    let who = Some(principal.name.as_str());
    if let Some(declared) = declared.filter(|n| *n > MAX_BODY as u64) {
        refuse(StatusCode::PAYLOAD_TOO_LARGE, who);
        if declared <= DRAIN_MAX {
            drain(body).await;
        }
        return too_large();
    }
    let mut bytes: Vec<u8> = Vec::new();
    while let Some(frame) = body.frame().await {
        let Ok(frame) = frame else {
            return detail(StatusCode::BAD_REQUEST, "could not read the request body");
        };
        if let Ok(data) = frame.into_data() {
            if bytes.len() + data.len() > MAX_BODY {
                refuse(StatusCode::PAYLOAD_TOO_LARGE, who);
                drain(body).await;
                return too_large();
            }
            bytes.extend_from_slice(&data);
        }
    }
    let mut parts = parts;
    parts.extensions.insert(Caller { who: principal.name.clone(), via });
    parts.extensions.insert(principal);
    next.run(Request::from_parts(parts, Body::from(bytes))).await
}

/// Who made the request, for the handlers' log lines (`config_reloaded` carries it).
#[derive(Debug, Clone)]
pub struct Caller {
    pub who: String,
    pub via: &'static str,
}

/// 413 for a body over the cap, closing the connection.
fn too_large() -> Response {
    closing(detail(StatusCode::PAYLOAD_TOO_LARGE, format!("request body larger than {MAX_BODY} bytes")))
}

/// `Connection: close`: the request's unread body cannot be skipped, so the connection ends.
fn closing(mut resp: Response) -> Response {
    resp.headers_mut().insert(header::CONNECTION, header::HeaderValue::from_static("close"));
    resp
}

/// Read and drop the rest of a refused body, up to `DRAIN_MAX` bytes or `DRAIN_TIME`. A server
/// that closes a socket with unread request bytes makes the kernel send a reset, and the
/// client then sees "connection reset" instead of the 413 (nginx: lingering close). Nothing
/// is kept, and only an authenticated caller gets this far.
async fn drain(mut body: Body) {
    let mut seen: u64 = 0;
    let _ = tokio::time::timeout(DRAIN_TIME, async {
        while let Some(Ok(frame)) = body.frame().await {
            if let Ok(data) = frame.into_data() {
                seen += data.len() as u64;
                if seen > DRAIN_MAX {
                    break;
                }
            }
        }
    })
    .await;
}

fn unknown(device_id: &str) -> Response {
    detail(StatusCode::NOT_FOUND, format!("unknown device {}", py_repr(device_id)))
}

/// Python `repr(str)` for the 404 text (`unknown device 'X'`).
fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

async fn health(State(st): St) -> Response {
    Json(st.view.health()).into_response()
}

/// Open: 200 while the process serves requests (a liveness probe).
async fn health_live() -> Response {
    Json(serde_json::json!({"status": "live"})).into_response()
}

/// Open: 200 once the config is loaded, the north side is started and every enabled device has
/// finished one cycle (or is demoted); 503 with the devices still waiting. Sticky (D-075).
async fn health_ready(State(st): St) -> Response {
    let state = st.view.readiness();
    let code = if state["ready"] == true { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE };
    (code, Json(state)).into_response()
}

async fn about(State(st): St) -> Response {
    Json(st.view.about()).into_response()
}

async fn support(State(st): St) -> Response {
    Json(st.view.support()).into_response()
}

async fn metrics(State(st): St) -> Response {
    ([(header::CONTENT_TYPE, METRICS_CONTENT_TYPE)], st.view.metrics()).into_response()
}

async fn channels(State(st): St) -> Response {
    Json(st.view.channels_summary()).into_response()
}

async fn system_tags(State(st): St, Query(q): Query<HashMap<String, String>>) -> Response {
    Json(st.view.system_tags(q.get("prefix").map(String::as_str).unwrap_or(""))).into_response()
}

/// The configuration API's own `/api/v1` routes (the legacy ones above keep their paths).
async fn v1_auth_info(State(st): St) -> Response {
    Json(json!({"open": st.auth.is_open()})).into_response()
}

async fn v1_whoami(State(st): St, principal: Option<axum::Extension<Principal>>) -> Response {
    let (name, role) = principal.map_or(("unknown".to_string(), Role::Viewer), |p| (p.0.name, p.0.role));
    Json(json!({
        "name": name,
        "role": if role == Role::Admin { "admin" } else { "viewer" },
        "open": st.auth.is_open(),
        "config_writable": false,
        "config_writable_reason": "configuration writes are not implemented in the Rust engine yet (#140)",
    }))
    .into_response()
}

async fn v1_status(State(st): St) -> Response {
    Json(json!({"health": st.view.health(), "channels": st.view.channels_summary(), "config": st.view.config_status()}))
        .into_response()
}

async fn v1_pending(State(st): St) -> Response {
    Json(st.view.config_status()).into_response()
}

async fn v1_device_get(
    State(st): St,
    Path(path): Path<String>,
    Query(q): Query<HashMap<String, String>>,
    principal: Option<axum::Extension<Principal>>,
) -> Response {
    if let Some(id) = path.strip_suffix("/status") {
        return match st.view.device_detail(id) {
            Some(v) => Json(v).into_response(),
            None => detail(StatusCode::NOT_FOUND, format!("device {} is not polled", py_repr(id))),
        };
    }
    if let Some(id) = path.strip_suffix("/values") {
        return match st.view.device_values(id) {
            Some(v) => Json(v).into_response(),
            None => detail(StatusCode::NOT_FOUND, format!("device {} is not polled", py_repr(id))),
        };
    }
    if let Some(id) = path.strip_suffix("/plan") {
        return match st.view.device_plan(id) {
            Some(v) => Json(v).into_response(),
            None => detail(StatusCode::NOT_FOUND, format!("device {} is not polled", py_repr(id))),
        };
    }
    if let Some(id) = path.strip_suffix("/identity") {
        let refresh = matches!(q.get("refresh").map(String::as_str), Some("true" | "1"));
        let admin = principal.is_some_and(|p| p.0.role == Role::Admin);
        return device_identity(&st, id, refresh, admin).await;
    }
    config_api::device_read(&st.config, &path, &q)
}

/// Identity over the open connection, cached for [`IDENTITY_TTL`]; `?refresh=true` (admin) asks again now.
async fn device_identity(st: &AppState, id: &str, refresh: bool, admin: bool) -> Response {
    if refresh && !admin {
        return detail(StatusCode::FORBIDDEN, "refreshing the identity needs the admin role");
    }
    let cached = {
        let cache = st.identity_cache.lock().unwrap_or_else(|e| e.into_inner());
        cache.get(id).filter(|(at, _)| !refresh && at.elapsed() < IDENTITY_TTL).cloned()
    };
    if let Some((at, mut v)) = cached {
        if let Value::Object(o) = &mut v {
            o.insert("cached".into(), json!(true));
            o.insert("age_s".into(), json!(at.elapsed().as_secs()));
        }
        return Json(v).into_response();
    }
    match st.view.device_identity(id).await {
        Ok(mut v) => {
            if v["connected"] == true {
                st.identity_cache
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .insert(id.to_string(), (std::time::Instant::now(), v.clone()));
            }
            if let Value::Object(o) = &mut v {
                o.insert("cached".into(), json!(false));
            }
            Json(v).into_response()
        }
        Err(ReadOnceError::UnknownDevice) => {
            detail(StatusCode::NOT_FOUND, format!("device {} is not polled", py_repr(id)))
        }
        // e.g. the safety timeout: report it, do not fail the request (as Python)
        Err(ReadOnceError::Failed(e)) => Json(json!({"connected": true, "identity": {}, "error": e})).into_response(),
    }
}

async fn v1_device_post(State(st): St, Path(path): Path<String>) -> Response {
    let Some(id) = path.strip_suffix("/read-once") else {
        return config_api::not_yet();
    };
    match st.view.read_once(id).await {
        Ok(v) => Json(v).into_response(),
        Err(ReadOnceError::UnknownDevice) => {
            detail(StatusCode::NOT_FOUND, format!("device {} is not polled", py_repr(id)))
        }
        Err(ReadOnceError::Failed(e)) => detail(StatusCode::CONFLICT, e),
    }
}

async fn device_get(State(st): St, Path(path): Path<String>, Query(q): Query<HashMap<String, String>>) -> Response {
    if let Some(id) = path.strip_suffix("/values") {
        return match st.view.device_values(id) {
            Some(mut v) => {
                if matches!(q.get("include_system").map(String::as_str), Some("true" | "1")) {
                    if let (Value::Object(o), Some(Value::Object(extra))) = (&mut v, st.view.device_system_values(id)) {
                        o.extend(extra);
                    }
                }
                Json(v).into_response()
            }
            None => unknown(id),
        };
    }
    match st.view.device_detail(&path) {
        Some(v) => Json(v).into_response(),
        None => unknown(&path),
    }
}

async fn device_post(State(st): St, Path(path): Path<String>) -> Response {
    let Some(id) = path.strip_suffix("/read-once") else {
        return detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    };
    match st.view.read_once(id).await {
        Ok(v) => Json(v).into_response(),
        Err(ReadOnceError::UnknownDevice) => unknown(id),
        Err(ReadOnceError::Failed(e)) => detail(StatusCode::INTERNAL_SERVER_ERROR, e),
    }
}

async fn reload(State(st): St, caller: Option<axum::Extension<Caller>>) -> Response {
    let result = match caller {
        Some(axum::Extension(c)) => st.view.reload_by(&c.who, c.via).await,
        None => st.view.reload().await,
    };
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => detail(StatusCode::BAD_REQUEST, format!("reload failed, old config kept: {e}")),
    }
}

async fn not_found() -> Response {
    detail(StatusCode::NOT_FOUND, "Not Found")
}

pub fn router(view: Arc<dyn EngineView>, auth: Auth) -> Router {
    router_with(view, auth, RefusalLog::default())
}

/// [`router`] with the refusal log given (tests inject a clock).
pub fn router_with(view: Arc<dyn EngineView>, auth: Auth, refusals: RefusalLog) -> Router {
    build(view, auth, refusals, None)
}

/// [`router`] with the configuration API's reads served from `config_dir` (`None`: they answer 503).
pub fn router_with_config(view: Arc<dyn EngineView>, auth: Auth, config_dir: Option<std::path::PathBuf>) -> Router {
    build(view, auth, RefusalLog::default(), config_dir)
}

fn build(
    view: Arc<dyn EngineView>,
    auth: Auth,
    refusals: RefusalLog,
    config_dir: Option<std::path::PathBuf>,
) -> Router {
    let config = config_dir.map(|d| Arc::new(opencap_core::config::reader::ConfigReader::new(d)));
    let state = Arc::new(AppState { view, auth, refusals, config: config.clone(), identity_cache: Default::default() });
    Router::new()
        .route("/health", get(health))
        .route("/health/live", get(health_live))
        .route("/health/ready", get(health_ready))
        .route("/api/v1/about", get(about))
        .route("/api/v1/support", get(support))
        .route("/api/v1/system/tags", get(system_tags))
        .route("/metrics", get(metrics))
        .route("/channels", get(channels))
        .route("/devices/{*path}", get(device_get).post(device_post))
        .route("/reload", post(reload))
        .route("/api/v1/reload", post(reload))
        .route("/api/v1/auth-info", get(v1_auth_info))
        .route("/api/v1/whoami", get(v1_whoami))
        .route("/api/v1/status", get(v1_status))
        .route("/api/v1/project/pending", get(v1_pending))
        .route(
            "/api/v1/devices/{*path}",
            get(v1_device_get)
                .post(v1_device_post)
                .put(|| async { config_api::not_yet() })
                .patch(|| async { config_api::not_yet() })
                .delete(|| async { config_api::not_yet() }),
        )
        .route("/", get(|| async { ui::to_ui() }))
        .route("/ui", get(|| async { ui::to_ui() }))
        .route("/ui/", get(|| async { ui::serve("") }))
        .route("/ui/{*path}", get(|Path(path): Path<String>| async move { ui::serve(&path) }))
        .fallback(not_found)
        .with_state(state.clone())
        .merge(config_api::routes(config))
        .layer(middleware::from_fn_with_state(state, guard))
}

/// Why the API could not bind (`api.bind_failed`, D-079): `reason` is one of `in_use`,
/// `permission_denied`, `address_unavailable`, `bad_address`, `config` and `io`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BindError {
    pub reason: &'static str,
    pub message: String,
}

impl std::fmt::Display for BindError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

/// Bind the API's listening socket, naming the reason when it fails.
pub async fn bind_api(host: &str, port: u16) -> Result<tokio::net::TcpListener, BindError> {
    use std::io::ErrorKind;
    let fail = |reason: &'static str, e: std::io::Error| BindError {
        reason,
        message: format!("cannot bind {host}:{port}: {e}"),
    };
    if let Err(e) = tokio::net::lookup_host((host, port)).await {
        return Err(fail("bad_address", e));
    }
    tokio::net::TcpListener::bind((host, port)).await.map_err(|e| {
        let reason = match e.kind() {
            ErrorKind::AddrInUse => "in_use",
            ErrorKind::PermissionDenied => "permission_denied",
            ErrorKind::AddrNotAvailable => "address_unavailable",
            _ => "io",
        };
        fail(reason, e)
    })
}

/// Bind and serve until `shutdown` resolves. Returns the bound address once listening.
pub async fn bind(bind: &str, port: u16) -> std::io::Result<tokio::net::TcpListener> {
    tokio::net::TcpListener::bind((bind, port)).await
}

pub async fn serve<F>(listener: tokio::net::TcpListener, app: Router, shutdown: F) -> std::io::Result<()>
where
    F: std::future::Future<Output = ()> + Send + 'static,
{
    // the peer address is what `api.request_refused` names as `client`
    axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>())
        .with_graceful_shutdown(shutdown)
        .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use futures_util::StreamExt;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tower::ServiceExt;

    const DEV: &str = "Site 001/8 PUMP";

    #[derive(Default)]
    struct FakePoller {
        reloads: AtomicUsize,
        identity_asks: AtomicUsize,
        connected: std::sync::atomic::AtomicBool,
    }

    #[async_trait]
    impl EngineView for FakePoller {
        fn health(&self) -> Value {
            json!({"status": "ok", "mqtt_connected": true})
        }
        fn channels_summary(&self) -> Value {
            json!([{"name": "Site 001", "devices": [{"id": DEV, "state": "ok"}]}])
        }
        fn device_detail(&self, id: &str) -> Option<Value> {
            (id == DEV).then(|| json!({"id": id, "state": "ok"}))
        }
        fn device_values(&self, id: &str) -> Option<Value> {
            (id == DEV).then(|| json!({"FLOW_1": {"value": 1.5, "quality": "good", "source_ts": null}}))
        }
        async fn read_once(&self, id: &str) -> Result<Value, ReadOnceError> {
            if id != DEV {
                return Err(ReadOnceError::UnknownDevice);
            }
            Ok(json!({"FLOW_1": {"value": 2.0, "quality": "good", "source_ts": null}}))
        }
        async fn reload(&self) -> Result<Value, String> {
            self.reloads.fetch_add(1, Ordering::SeqCst);
            Ok(json!({"started": [], "restarted": ["X"], "stopped": [], "unchanged": []}))
        }
        fn metrics(&self) -> String {
            "# HELP poll_requests_total x\n".into()
        }
        fn readiness(&self) -> Value {
            let ready = self.reloads.load(Ordering::SeqCst) > 0; // the tests flip it with a reload
            json!({"ready": ready, "waiting": if ready { json!([]) } else { json!([DEV]) }})
        }
        fn about(&self) -> Value {
            json!({"product": "opencap", "engine": "rust"})
        }
        fn support(&self) -> Value {
            json!({"product": "opencap", "config_revision": "0123456789abcdef"})
        }
        fn device_plan(&self, id: &str) -> Option<Value> {
            (id == DEV).then(|| json!({"id": id, "blocks": 1, "block_plan": []}))
        }
        async fn device_identity(&self, id: &str) -> Result<Value, ReadOnceError> {
            if id == "Site 001/BOOM" {
                return Err(ReadOnceError::Failed("the identity request timed out".into()));
            }
            if id != DEV {
                return Err(ReadOnceError::UnknownDevice);
            }
            self.identity_asks.fetch_add(1, Ordering::SeqCst);
            let connected = self.connected.load(Ordering::SeqCst);
            Ok(
                json!({"connected": connected, "identity": if connected { json!({"vendor": "ACME"}) } else { json!({}) }}),
            )
        }
    }

    async fn call(app: &Router, method: &str, path: &str, token: Option<&str>) -> (StatusCode, Value) {
        call_host(app, method, path, token, "127.0.0.1:8080").await
    }

    async fn call_host(app: &Router, method: &str, path: &str, token: Option<&str>, host: &str) -> (StatusCode, Value) {
        let resp = send(app, method, path, token, host).await;
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::String(String::from_utf8_lossy(&bytes).into())))
    }

    async fn send(app: &Router, method: &str, path: &str, token: Option<&str>, host: &str) -> Response {
        let uri = path.replace(' ', "%20");
        let mut req = Request::builder().method(method).uri(uri).header("Host", host);
        if let Some(t) = token {
            req = req.header("Authorization", format!("Bearer {t}"));
        }
        app.clone().oneshot(req.body(Body::empty()).unwrap()).await.unwrap()
    }

    fn app() -> (Router, Arc<FakePoller>) {
        let fake = Arc::new(FakePoller::default());
        (router(fake.clone(), Auth::single("s3cret")), fake)
    }

    fn spec(name: &str, role: Role, env: &str) -> TokenSpec {
        TokenSpec { name: name.into(), role, token_env: env.into() }
    }

    fn env(vars: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<String> {
        move |k| vars.iter().find(|(n, _)| *n == k).map(|(_, v)| v.to_string())
    }

    #[tokio::test]
    async fn the_ui_bundle_is_open_with_security_headers() {
        let (a, _) = app();
        let resp = send(&a, "GET", "/ui/", None, "127.0.0.1").await;
        assert_eq!(resp.status(), StatusCode::OK);
        let h = resp.headers();
        assert!(h["content-type"].to_str().unwrap().starts_with("text/html"));
        assert!(h["content-security-policy"].to_str().unwrap().contains("script-src 'self'"));
        assert_eq!(
            (h["x-frame-options"].to_str().unwrap(), h["cache-control"].to_str().unwrap()),
            ("DENY", "no-cache")
        );
        let body = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
        assert!(String::from_utf8_lossy(&body).contains("<html"));
        for (path, mime) in [("/ui/app.js", "javascript"), ("/ui/app.css", "css"), ("/ui/i18n/en.json", "json")] {
            let r = send(&a, "GET", path, None, "127.0.0.1").await;
            assert_eq!(r.status(), StatusCode::OK, "{path}");
            assert!(r.headers()["content-type"].to_str().unwrap().contains(mime), "{path}");
        }
        assert_eq!(send(&a, "GET", "/ui/nope.js", None, "127.0.0.1").await.status(), StatusCode::NOT_FOUND);
        for path in ["/", "/ui"] {
            let r = send(&a, "GET", path, None, "127.0.0.1").await;
            assert_eq!(
                (r.status(), r.headers()["location"].to_str().unwrap()),
                (StatusCode::TEMPORARY_REDIRECT, "/ui/")
            );
        }
    }

    fn config_app() -> (Router, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!("opencap-api-cfg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let tag = |n: &str| json!({"name": n, "address": "400001", "dtype": "int16"});
        let dev = |n: &str, tags: Vec<Value>| {
            json!({"id": format!("Site 001/{n}"), "channel": "Site 001", "name": n, "driver": "modbus_tcp",
                   "host": "192.0.2.1", "port": 502, "tags": tags})
        };
        std::fs::write(
            dir.join("devices.json"),
            json!({"channels": [{"name": "Site 001", "devices": [dev("PUMP2", vec![tag("A"), tag("B.C")]), dev("PUMP10", vec![])]}]})
                .to_string(),
        )
        .unwrap();
        std::fs::write(dir.join("devices_map.yaml"), "devices:\n  Site 001/PUMP2: {sub_device_id: SUB_2}\n").unwrap();
        let fake = Arc::new(FakePoller::default());
        (router_with_config(fake, Auth::single("s3cret"), Some(dir.clone())), dir)
    }

    #[tokio::test]
    async fn the_configuration_reads_carry_the_revision_and_the_etag() {
        let (a, dir) = config_app();
        let t = Some("s3cret");
        let resp = send(&a, "GET", "/api/v1/channels", t, "127.0.0.1").await;
        assert_eq!(resp.status(), StatusCode::OK);
        let etag = resp.headers()[header::ETAG].to_str().unwrap().to_string();
        let body: Value =
            serde_json::from_slice(&axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap()).unwrap();
        assert_eq!(etag, format!("\"{}\"", body["revision"].as_str().unwrap()));
        assert_eq!((body["total"].as_i64(), body["channels"][0]["devices"].as_i64()), (Some(1), Some(2)));
        // list queries: natural sort, paging, and a 422 for a field the collection does not offer
        let (_, devs) = call(&a, "GET", "/api/v1/devices?sort=name&order=desc&limit=1", t).await;
        assert_eq!(
            (devs["devices"][0]["name"].as_str(), devs["total"].as_i64(), devs["limit"].as_i64()),
            (Some("PUMP10"), Some(2), Some(1))
        );
        let (code, bad) = call(&a, "GET", "/api/v1/channels?sort=port", t).await;
        assert_eq!(
            (code, bad["detail"][0]["loc"].clone()),
            (StatusCode::UNPROCESSABLE_ENTITY, json!(["query", "sort"]))
        );
        assert_eq!(call(&a, "GET", "/api/v1/devices?limit=0", t).await.0, StatusCode::UNPROCESSABLE_ENTITY);
        // device ids contain a slash and a space; the runtime suffixes still win
        let (_, dev) = call(&a, "GET", "/api/v1/devices/Site 001/PUMP2", t).await;
        assert_eq!(
            (dev["device"]["host"].as_str(), dev["map_entry"]["sub_device_id"].as_str()),
            (Some("192.0.2.1"), Some("SUB_2"))
        );
        let (_, tags) = call(&a, "GET", "/api/v1/devices/Site 001/PUMP2/tags?q=b", t).await;
        assert_eq!((tags["total"].as_i64(), tags["tags"][0]["name"].as_str()), (Some(1), Some("B.C")));
        assert_eq!(call(&a, "GET", "/api/v1/devices/Site 001/PUMP2/tags/B.C", t).await.1["tag"]["dtype"], "int16");
        assert_eq!(call(&a, "GET", "/api/v1/devices/Site 001/NOPE", t).await.0, StatusCode::NOT_FOUND);
        let (code, nf) = call(&a, "GET", "/api/v1/channels/Nope", t).await;
        assert_eq!((code, nf["detail"].as_str()), (StatusCode::NOT_FOUND, Some("unknown channel 'Nope'")));
        assert_eq!(call(&a, "GET", "/api/v1/channels/Site 001/devices", t).await.1["devices"][1]["name"], "PUMP10");
        assert_eq!(call(&a, "GET", "/api/v1/map/devices/Site 001/PUMP2", t).await.1["entry"]["sub_device_id"], "SUB_2");
        assert_eq!(call(&a, "GET", "/api/v1/map", t).await.1["devices"]["Site 001/PUMP2"]["sub_device_id"], "SUB_2");
        assert_eq!(call(&a, "GET", "/api/v1/overrides", t).await.1["devices"], json!({}));
        assert_eq!(call(&a, "GET", "/api/v1/overrides/devices/Site 001/PUMP2", t).await.0, StatusCode::NOT_FOUND);
        let (_, status) = call(&a, "GET", "/api/v1/project/status", t).await;
        assert_eq!((status["counts"]["devices"].as_i64(), status["valid"].as_bool()), (Some(2), Some(true)));
        let resp = send(&a, "GET", "/api/v1/project?download=true", t, "127.0.0.1").await;
        assert!(resp.headers()[header::CONTENT_DISPOSITION]
            .to_str()
            .unwrap()
            .starts_with("attachment; filename=\"opencap-config-"));
        assert_eq!(call(&a, "GET", "/api/v1/audit", t).await.1["entries"], json!([]));
        // writes are not ported: said plainly, and the guard still refuses a viewer first
        assert_eq!(call(&a, "POST", "/api/v1/channels", t).await.0, StatusCode::NOT_IMPLEMENTED);
        assert_eq!(call(&a, "PATCH", "/api/v1/devices/Site 001/PUMP2", t).await.0, StatusCode::NOT_IMPLEMENTED);
        assert_eq!(call(&a, "GET", "/api/v1/channels", None).await.0, StatusCode::UNAUTHORIZED);
        // without a config directory: 503, as Python's Unavailable
        let (bare, _) = app();
        assert_eq!(call(&bare, "GET", "/api/v1/channels", t).await.0, StatusCode::SERVICE_UNAVAILABLE);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn v1_runtime_routes_mirror_the_legacy_ones() {
        let (a, fake) = app();
        let t = Some("s3cret");
        assert_eq!(call(&a, "GET", "/api/v1/auth-info", None).await.1, json!({"open": false}), "open route");
        let (code, who) = call(&a, "GET", "/api/v1/whoami", t).await;
        assert_eq!(
            (code, who["name"].as_str(), who["role"].as_str(), who["config_writable"].as_bool()),
            (StatusCode::OK, Some("default"), Some("admin"), Some(false))
        );
        assert_eq!(call(&a, "GET", "/api/v1/whoami", None).await.0, StatusCode::UNAUTHORIZED);
        let (_, status) = call(&a, "GET", "/api/v1/status", t).await;
        assert_eq!(status["health"]["status"], "ok");
        assert_eq!(status["channels"][0]["name"], "Site 001");
        assert_eq!(status["config"]["pending_reload"], false);
        assert_eq!(call(&a, "GET", "/api/v1/project/pending", t).await.1["available"], false);
        assert_eq!(call(&a, "GET", &format!("/api/v1/devices/{DEV}/status"), t).await.1["id"], DEV);
        assert_eq!(call(&a, "GET", &format!("/api/v1/devices/{DEV}/values"), t).await.1["FLOW_1"]["value"], 1.5);
        assert_eq!(call(&a, "POST", &format!("/api/v1/devices/{DEV}/read-once"), t).await.1["FLOW_1"]["value"], 2.0);
        assert_eq!(call(&a, "GET", "/api/v1/devices/NOPE/X/status", t).await.0, StatusCode::NOT_FOUND);
        assert_eq!(call(&a, "POST", "/api/v1/reload", t).await.1["restarted"][0], "X");
        assert_eq!(fake.reloads.load(Ordering::SeqCst), 1);
        // no config directory in this app: the configuration reads answer 503, not 404
        assert_eq!(call(&a, "GET", &format!("/api/v1/devices/{DEV}"), t).await.0, StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn plan_and_identity_with_the_python_semantics() {
        let (a, fake) = app();
        let t = Some("s3cret");
        let plan = call(&a, "GET", &format!("/api/v1/devices/{DEV}/plan"), t).await;
        assert_eq!((plan.0, plan.1["blocks"].as_u64()), (StatusCode::OK, Some(1)));
        assert_eq!(call(&a, "GET", "/api/v1/devices/NOPE/X/plan", t).await.0, StatusCode::NOT_FOUND);
        assert_eq!(call(&a, "GET", "/api/v1/devices/NOPE/X/identity", t).await.0, StatusCode::NOT_FOUND);
        // not connected: answered, never cached (the next ask goes to the engine again)
        let id = format!("/api/v1/devices/{DEV}/identity");
        let (_, v) = call(&a, "GET", &id, t).await;
        assert_eq!((v["connected"].as_bool(), v["cached"].as_bool()), (Some(false), Some(false)));
        call(&a, "GET", &id, t).await;
        assert_eq!(fake.identity_asks.load(Ordering::SeqCst), 2);
        // connected: asked once, then served from the cache with its age
        fake.connected.store(true, Ordering::SeqCst);
        let (_, v) = call(&a, "GET", &id, t).await;
        assert_eq!((v["identity"]["vendor"].as_str(), v["cached"].as_bool()), (Some("ACME"), Some(false)));
        let (_, v) = call(&a, "GET", &id, t).await;
        assert_eq!((v["cached"].as_bool(), v["age_s"].as_u64()), (Some(true), Some(0)));
        assert_eq!(fake.identity_asks.load(Ordering::SeqCst), 3);
        // refresh asks again (admin)
        let (_, v) = call(&a, "GET", &format!("{id}?refresh=true"), t).await;
        assert_eq!(v["cached"].as_bool(), Some(false));
        assert_eq!(fake.identity_asks.load(Ordering::SeqCst), 4);
        // an engine error is reported in the body, not as a failure
        let (code, v) = call(&a, "GET", "/api/v1/devices/Site 001/BOOM/identity", t).await;
        assert_eq!(
            (code, v["connected"].as_bool(), v["error"].as_str()),
            (StatusCode::OK, Some(true), Some("the identity request timed out"))
        );
    }

    #[test]
    fn a_viewer_may_not_refresh_the_identity() {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        rt.block_on(async {
            let fake = Arc::new(FakePoller::default());
            let auth = Auth::new(None, &[spec("v", Role::Viewer, "V")], env(&[("V", "vtok")])).unwrap();
            let a = router(fake, auth);
            let id = format!("/api/v1/devices/{DEV}/identity");
            assert_eq!(call(&a, "GET", &id, Some("vtok")).await.0, StatusCode::OK);
            assert_eq!(call(&a, "GET", &format!("{id}?refresh=true"), Some("vtok")).await.0, StatusCode::FORBIDDEN);
        });
    }

    #[tokio::test]
    async fn health_and_metrics_are_open() {
        let (a, _) = app();
        assert_eq!(call(&a, "GET", "/health", None).await.1["status"], "ok");
        let (code, body) = call(&a, "GET", "/metrics", None).await;
        assert_eq!(code, StatusCode::OK);
        assert!(body.as_str().unwrap().contains("poll_requests_total"));
    }

    #[tokio::test]
    async fn token_required() {
        let (a, _) = app();
        for (m, p) in [
            ("GET", "/channels".to_string()),
            ("GET", format!("/devices/{DEV}")),
            ("GET", format!("/devices/{DEV}/values")),
            ("POST", format!("/devices/{DEV}/read-once")),
            ("POST", "/reload".to_string()),
        ] {
            let (code, body) = call(&a, m, &p, None).await;
            assert_eq!(code, StatusCode::UNAUTHORIZED, "{m} {p}");
            assert_eq!(body["detail"], "missing or bad bearer token");
            assert_eq!(call(&a, m, &p, Some("wrong")).await.0, StatusCode::UNAUTHORIZED);
            assert_eq!(call(&a, m, &p, Some("s3cret")).await.0, StatusCode::OK, "{m} {p}");
        }
    }

    #[tokio::test]
    async fn device_routes_with_slash_and_spaces() {
        let (a, fake) = app();
        let t = Some("s3cret");
        assert_eq!(call(&a, "GET", &format!("/devices/{DEV}"), t).await.1["id"], DEV);
        assert_eq!(call(&a, "GET", &format!("/devices/{DEV}/values"), t).await.1["FLOW_1"]["value"], 1.5);
        assert_eq!(call(&a, "POST", &format!("/devices/{DEV}/read-once"), t).await.1["FLOW_1"]["value"], 2.0);
        assert_eq!(call(&a, "POST", "/reload", t).await.1["restarted"][0], "X");
        assert_eq!(fake.reloads.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn unknown_device_404() {
        let (a, _) = app();
        let t = Some("s3cret");
        let (code, body) = call(&a, "GET", "/devices/NOPE/X", t).await;
        assert_eq!((code, body["detail"].as_str().unwrap()), (StatusCode::NOT_FOUND, "unknown device 'NOPE/X'"));
        assert_eq!(call(&a, "GET", "/devices/NOPE/X/values", t).await.0, StatusCode::NOT_FOUND);
        assert_eq!(call(&a, "POST", "/devices/NOPE/X/read-once", t).await.0, StatusCode::NOT_FOUND);
        assert_eq!(call(&a, "GET", "/nothing", t).await.0, StatusCode::NOT_FOUND);
    }

    #[test]
    fn no_token_on_public_bind_refused() {
        let open = Auth::default();
        assert!(check_bind("192.0.2.11", &open, "OPENCAP_NO_TOKEN").is_err());
        assert!(check_bind("127.0.0.1", &open, "X").is_ok());
        assert!(check_bind("localhost", &open, "X").is_ok());
        assert!(check_bind("::1", &open, "X").is_ok());
        assert!(check_bind("192.0.2.11", &Auth::single("t"), "X").is_ok());
        let named = Auth::new(None, &[spec("ops", Role::Admin, "T")], env(&[("T", "x")])).unwrap();
        assert!(check_bind("192.0.2.11", &named, "X").is_ok());
    }

    #[tokio::test]
    async fn no_token_is_read_only_and_loopback_only() {
        let fake = Arc::new(FakePoller::default());
        let a = router(fake.clone(), Auth::default());
        for host in ["127.0.0.1:8080", "localhost:8080", "[::1]:8080", "LOCALHOST"] {
            assert_eq!(call_host(&a, "GET", "/channels", None, host).await.0, StatusCode::OK, "{host}");
        }
        // exactly Python's LOOPBACK_HOSTS: other 127/8 names and look-alikes are refused
        for host in ["127.0.0.2:8080", "localhost.evil.example", "0.0.0.0:8080"] {
            assert_eq!(call_host(&a, "GET", "/channels", None, host).await.0, StatusCode::FORBIDDEN, "{host}");
        }
        // DNS rebinding: a foreign page reaching the loopback API sends its own host name
        let (code, body) = call_host(&a, "GET", "/channels", None, "evil.example:8080").await;
        assert_eq!(code, StatusCode::FORBIDDEN);
        assert_eq!(body["detail"], "no API token is configured: only requests to localhost are answered");
        for (m, p) in [("POST", "/reload".to_string()), ("POST", format!("/devices/{DEV}/read-once"))] {
            let (code, body) = call(&a, m, &p, None).await;
            assert_eq!(code, StatusCode::FORBIDDEN, "{p}");
            assert!(body["detail"]
                .as_str()
                .unwrap()
                .starts_with("no API token is configured, so the API is read-only"));
        }
        assert_eq!(fake.reloads.load(Ordering::SeqCst), 0);
        assert_eq!(call_host(&a, "GET", "/health", None, "evil.example").await.0, StatusCode::OK);
    }

    #[tokio::test]
    async fn named_tokens_and_roles() {
        let auth = Auth::new(
            Some("legacy-secret".into()),
            &[spec("ops", Role::Admin, "OPS"), spec("wall", Role::Viewer, "WALL")],
            env(&[("OPS", "ops-secret"), ("WALL", "wall-secret")]),
        )
        .unwrap();
        let fake = Arc::new(FakePoller::default());
        let a = router(fake.clone(), auth);
        for t in ["legacy-secret", "ops-secret", "wall-secret"] {
            assert_eq!(call(&a, "GET", "/channels", Some(t)).await.0, StatusCode::OK, "{t}");
        }
        let (code, body) = call(&a, "POST", "/reload", Some("wall-secret")).await;
        assert_eq!(
            (code, body["detail"].as_str().unwrap()),
            (StatusCode::FORBIDDEN, "token 'wall' has role viewer: read-only")
        );
        assert_eq!(
            call(&a, "POST", &format!("/devices/{DEV}/read-once"), Some("wall-secret")).await.0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(call(&a, "POST", "/reload", Some("ops-secret")).await.0, StatusCode::OK);
        assert_eq!(call(&a, "POST", "/reload", Some("legacy-secret")).await.0, StatusCode::OK);
        assert_eq!(fake.reloads.load(Ordering::SeqCst), 2);
        let resp = send(&a, "GET", "/channels", Some("nope"), "127.0.0.1").await;
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(resp.headers()[header::WWW_AUTHENTICATE], "Bearer");
        // with tokens configured, any Host is answered (the token is the protection)
        assert_eq!(call_host(&a, "GET", "/channels", Some("wall-secret"), "192.0.2.30:8080").await.0, StatusCode::OK);
    }

    async fn send_body(app: &Router, path: &str, token: Option<&str>, body: Body, length: Option<usize>) -> Response {
        let mut req = Request::builder().method("POST").uri(path).header("Host", "127.0.0.1");
        if let Some(t) = token {
            req = req.header("Authorization", format!("Bearer {t}"));
        }
        if let Some(n) = length {
            req = req.header("Content-Length", n.to_string());
        }
        app.clone().oneshot(req.body(body).unwrap()).await.unwrap()
    }

    #[tokio::test]
    async fn auth_before_body_and_body_cap() {
        let (a, fake) = app();
        // a malformed body from an unauthenticated caller: 401, the body is never read
        let resp = send_body(&a, "/reload", None, Body::from("{not json"), Some(9)).await;
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        // even a huge one: 401 before the size check, as in Python
        let resp = send_body(&a, "/reload", Some("wrong"), Body::empty(), Some(5 * MAX_BODY)).await;
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        // authenticated, Content-Length over the cap: 413 at once
        let resp = send_body(&a, "/reload", Some("s3cret"), Body::empty(), Some(MAX_BODY + 1)).await;
        assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE);
        // chunked (no Content-Length) past the cap: counted while read, 413
        let chunks: Vec<Result<Vec<u8>, std::io::Error>> = (0..17).map(|_| Ok(vec![b'x'; 64 * 1024])).collect();
        let stream = futures_util::stream::iter(chunks);
        let resp = send_body(&a, "/reload", Some("s3cret"), Body::from_stream(stream), None).await;
        assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE);
        let bytes = axum::body::to_bytes(resp.into_body(), 1 << 16).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&bytes).unwrap()["detail"],
            "request body larger than 1048576 bytes"
        );
        assert_eq!(fake.reloads.load(Ordering::SeqCst), 0);
        // a small body is fine
        let resp = send_body(&a, "/reload", Some("s3cret"), Body::from("{}"), Some(2)).await;
        assert_eq!(resp.status(), StatusCode::OK);
        // unknown paths need a token too; open paths do not
        assert_eq!(call(&a, "GET", "/nothing", None).await.0, StatusCode::UNAUTHORIZED);
        assert_eq!(call(&a, "GET", "/api/v1/auth-info", None).await.0, StatusCode::OK);
        assert_eq!(call(&a, "GET", "/api/v1/nothing", None).await.0, StatusCode::UNAUTHORIZED);
        assert!(is_open_path("/ui/app.js") && !is_open_path("/uix") && !is_open_path("/channels"));
    }

    #[tokio::test(start_paused = true)]
    async fn refused_bodies_are_drained_within_bounds() {
        let (a, _) = app();
        // the 413 closes the connection: the rest of the body cannot be skipped
        let resp = send_body(&a, "/reload", Some("s3cret"), Body::empty(), Some(MAX_BODY + 1)).await;
        assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(resp.headers()[header::CONNECTION], "close");
        // a client that stops sending mid-body does not hold the request past DRAIN_TIME
        let stalled = futures_util::stream::iter(vec![Ok::<_, std::io::Error>(vec![b'x'; MAX_BODY + 1])])
            .chain(futures_util::stream::pending());
        let started = tokio::time::Instant::now();
        let resp = send_body(&a, "/reload", Some("s3cret"), Body::from_stream(stalled), None).await;
        assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE);
        assert!(started.elapsed() >= DRAIN_TIME && started.elapsed() < DRAIN_TIME * 2, "{:?}", started.elapsed());
        // declared larger than DRAIN_MAX: refused without reading a byte (the stream never ends)
        let endless = futures_util::stream::pending::<Result<Vec<u8>, std::io::Error>>();
        let started = tokio::time::Instant::now();
        let declared = usize::try_from(DRAIN_MAX).unwrap() + 1;
        let resp = send_body(&a, "/reload", Some("s3cret"), Body::from_stream(endless), Some(declared)).await;
        assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(started.elapsed(), Duration::ZERO);
    }

    #[tokio::test(start_paused = true)]
    async fn open_paths_and_options_take_no_body() {
        let (a, _) = app();
        let req = |method: &str, path: &str, body: Body, length: Option<usize>| {
            let mut r = Request::builder().method(method).uri(path).header("Host", "127.0.0.1");
            if let Some(n) = length {
                r = r.header("Content-Length", n.to_string());
            }
            r.body(body).unwrap()
        };
        assert_eq!(
            a.clone().oneshot(req("GET", "/health", Body::empty(), None)).await.unwrap().status(),
            StatusCode::OK
        );
        assert_eq!(
            a.clone().oneshot(req("GET", "/health", Body::empty(), Some(0))).await.unwrap().status(),
            StatusCode::OK
        );
        // a body on an open path or OPTIONS is refused at once and never read: the stream
        // below never ends, so reading it would hang (the clock is paused)
        for (method, path, length) in
            [("GET", "/health", Some(5)), ("GET", "/metrics", None), ("OPTIONS", "/reload", Some(3))]
        {
            let endless = futures_util::stream::pending::<Result<Vec<u8>, std::io::Error>>();
            let started = tokio::time::Instant::now();
            let resp = a.clone().oneshot(req(method, path, Body::from_stream(endless), length)).await.unwrap();
            assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE, "{method} {path}");
            assert_eq!(resp.headers()[header::CONNECTION], "close");
            assert_eq!(started.elapsed(), Duration::ZERO);
        }
    }

    #[test]
    fn token_set_up_errors_match_python() {
        let e = Auth::new(None, &[spec("ops", Role::Admin, "UNSET")], env(&[])).unwrap_err();
        assert_eq!(e, "api.tokens[0] 'ops': env var UNSET is not set");
        let e = Auth::new(None, &[spec("ops", Role::Admin, "EMPTY")], env(&[("EMPTY", "")])).unwrap_err();
        assert!(e.contains("is not set"), "{e}");
        let e = Auth::new(Some("same".into()), &[spec("ops", Role::Admin, "A")], env(&[("A", "same")])).unwrap_err();
        assert_eq!(e, "api tokens: two tokens have the same secret; give each its own");
        assert!(Auth::new(Some(String::new()), &[], env(&[])).unwrap().is_open());
    }

    #[test]
    fn host_names() {
        assert_eq!(host_name("[::1]:8080"), "::1");
        assert_eq!(host_name("localhost:8080"), "localhost");
        assert_eq!(host_name(" 127.0.0.1 "), "127.0.0.1");
        assert_eq!(host_name("::1"), "::1");
        assert_eq!(host_name(""), "");
    }

    #[tokio::test]
    async fn readiness_about_and_support() {
        let (a, fake) = app();
        for open in ["/health/live", "/health/ready", "/api/v1/about"] {
            assert!(is_open_path(open), "{open}");
        }
        let (code, body) = call(&a, "GET", "/health/ready", None).await;
        assert_eq!(
            (code, body["ready"].clone(), body["waiting"].clone()),
            (StatusCode::SERVICE_UNAVAILABLE, json!(false), json!([DEV]))
        );
        assert_eq!(call(&a, "GET", "/health/live", None).await.1, json!({"status": "live"}));
        assert_eq!(call(&a, "GET", "/api/v1/about", None).await.1["engine"], "rust", "open, no token");
        assert_eq!(call(&a, "GET", "/api/v1/support", None).await.0, StatusCode::UNAUTHORIZED, "viewer, not open");
        assert_eq!(call(&a, "GET", "/api/v1/support", Some("s3cret")).await.1["config_revision"], "0123456789abcdef");
        fake.reloads.fetch_add(1, Ordering::SeqCst);
        assert_eq!(call(&a, "GET", "/health/ready", None).await.0, StatusCode::OK);
    }
}
