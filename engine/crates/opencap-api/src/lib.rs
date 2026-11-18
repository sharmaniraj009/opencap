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

use std::net::IpAddr;
use std::sync::Arc;

use async_trait::async_trait;
use axum::body::Body;
use axum::extract::{Path, Request, State};
use axum::http::{header, HeaderMap, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

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
    async fn read_once(&self, device_id: &str) -> Result<Value, ReadOnceError>;
    /// Err = the new config was rejected; the old one is kept.
    async fn reload(&self) -> Result<Value, String>;
    /// Prometheus text exposition.
    fn metrics(&self) -> String;
}

pub const METRICS_CONTENT_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";

pub fn is_loopback(host: &str) -> bool {
    host == "localhost" || host.parse::<IpAddr>().map(|ip| ip.is_loopback()).unwrap_or(false)
}

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

    /// Authorize a request (Python `Auth.principal`): Err is the 401/403 response.
    #[allow(clippy::result_large_err)] // the Err is the response itself, built once per request
    pub fn principal(&self, method: &Method, headers: &HeaderMap) -> Result<Principal, Response> {
        let safe = matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS);
        if self.is_open() {
            let host = headers.get(header::HOST).and_then(|v| v.to_str().ok()).unwrap_or("");
            if !is_loopback(&host_name(host)) {
                return Err(detail(
                    StatusCode::FORBIDDEN,
                    "no API token is configured: only requests to localhost are answered",
                ));
            }
            if !safe {
                return Err(detail(StatusCode::FORBIDDEN, OPEN_READ_ONLY));
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
            return Err(resp);
        };
        if !safe && role != Role::Admin {
            return Err(detail(StatusCode::FORBIDDEN, format!("token {} has role viewer: read-only", py_repr(&name))));
        }
        Ok(Principal { name, role })
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
}

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

/// Paths answered without a token (Python `guard.OPEN_PATHS` / `OPEN_PREFIXES`). The Rust
/// engine serves only `/health` and `/metrics` of these; the rest answer 404 without a token,
/// as they would in Python on a build without the UI.
const OPEN_PATHS: [&str; 5] = ["/health", "/metrics", "/api/v1/auth-info", "/", "/ui"];
const OPEN_PREFIX: &str = "/ui/";
/// Request body cap (Python `guard.MAX_BODY`).
pub const MAX_BODY: usize = 1024 * 1024;

pub fn is_open_path(path: &str) -> bool {
    OPEN_PATHS.contains(&path) || path.starts_with(OPEN_PREFIX)
}

/// Python `ApiGuard`: authenticate from the headers before a single body byte is read, then
/// cap the body (413 on a `Content-Length` over the cap at once, or while a chunked body is
/// read). Runs in front of every route and the fallback.
async fn guard(State(st): St, req: Request, next: Next) -> Response {
    let (parts, body) = req.into_parts();
    if !is_open_path(parts.uri.path()) && parts.method != Method::OPTIONS {
        if let Err(refused) = st.auth.principal(&parts.method, &parts.headers) {
            return refused;
        }
    }
    let too_large = || detail(StatusCode::PAYLOAD_TOO_LARGE, format!("request body larger than {MAX_BODY} bytes"));
    let length = parts.headers.get(header::CONTENT_LENGTH).and_then(|v| v.to_str().ok());
    if length.and_then(|v| v.trim().parse::<u64>().ok()).is_some_and(|n| n > MAX_BODY as u64) {
        return too_large();
    }
    let bytes = match axum::body::to_bytes(body, MAX_BODY).await {
        Ok(bytes) => bytes,
        Err(e) if is_length_limit(&e) => return too_large(),
        Err(_) => return detail(StatusCode::BAD_REQUEST, "could not read the request body"),
    };
    next.run(Request::from_parts(parts, Body::from(bytes))).await
}

/// The body passed the cap (not a broken connection).
fn is_length_limit(e: &axum::Error) -> bool {
    std::error::Error::source(e).is_some_and(|inner| inner.is::<http_body_util::LengthLimitError>())
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

async fn metrics(State(st): St) -> Response {
    ([(header::CONTENT_TYPE, METRICS_CONTENT_TYPE)], st.view.metrics()).into_response()
}

async fn channels(State(st): St) -> Response {
    Json(st.view.channels_summary()).into_response()
}

async fn device_get(State(st): St, Path(path): Path<String>) -> Response {
    if let Some(id) = path.strip_suffix("/values") {
        return match st.view.device_values(id) {
            Some(v) => Json(v).into_response(),
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

async fn reload(State(st): St) -> Response {
    match st.view.reload().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => detail(StatusCode::BAD_REQUEST, format!("reload failed, old config kept: {e}")),
    }
}

async fn not_found() -> Response {
    detail(StatusCode::NOT_FOUND, "Not Found")
}

pub fn router(view: Arc<dyn EngineView>, auth: Auth) -> Router {
    let state = Arc::new(AppState { view, auth });
    Router::new()
        .route("/health", get(health))
        .route("/metrics", get(metrics))
        .route("/channels", get(channels))
        .route("/devices/{*path}", get(device_get).post(device_post))
        .route("/reload", post(reload))
        .fallback(not_found)
        .layer(middleware::from_fn_with_state(state.clone(), guard))
        .with_state(state)
}

/// Bind and serve until `shutdown` resolves. Returns the bound address once listening.
pub async fn bind(bind: &str, port: u16) -> std::io::Result<tokio::net::TcpListener> {
    tokio::net::TcpListener::bind((bind, port)).await
}

pub async fn serve<F>(listener: tokio::net::TcpListener, app: Router, shutdown: F) -> std::io::Result<()>
where
    F: std::future::Future<Output = ()> + Send + 'static,
{
    axum::serve(listener, app).with_graceful_shutdown(shutdown).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tower::ServiceExt;

    const DEV: &str = "Site 001/8 PUMP";

    #[derive(Default)]
    struct FakePoller {
        reloads: AtomicUsize,
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
        assert_eq!(call(&a, "GET", "/api/v1/auth-info", None).await.0, StatusCode::NOT_FOUND);
        assert!(is_open_path("/ui/app.js") && !is_open_path("/uix") && !is_open_path("/channels"));
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
}
