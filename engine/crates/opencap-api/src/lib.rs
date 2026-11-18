//! Admin REST API: the same routes, auth and status codes as `src/opencap/api/server.py`.
//! Bind to localhost or the VPN only.
//!
//! - `GET /health`, `GET /metrics`: open (monitoring).
//! - `GET /channels`, `GET /devices/{id}`, `GET /devices/{id}/values`,
//!   `POST /devices/{id}/read-once`, `POST /reload`: `Authorization: Bearer <token>`.
//! - The token comes from the env var named by `api.token_env`. With no token the API
//!   refuses to start unless bound to a loopback address.
//! - Device ids contain `/` (`<channel>/<device>`), so the `/values` and `/read-once`
//!   suffixes are matched first, as in the Python routes.
//!
//! The engine implements [`EngineView`]; handlers never wait on PLC I/O except
//! `read-once`, which is the one route that is supposed to.

use std::net::IpAddr;
use std::sync::Arc;

use async_trait::async_trait;
use axum::extract::{Path, State};
use axum::http::{header, HeaderMap, StatusCode};
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
pub fn check_bind(bind: &str, token: Option<&str>, token_env: &str) -> Result<(), String> {
    if token.is_none() && !is_loopback(bind) {
        return Err(format!("API bound to {bind} without a token: set {token_env} or bind to 127.0.0.1"));
    }
    Ok(())
}

/// Read the token from the env var (empty = unset, as Python's `os.environ.get(...) or None`).
pub fn token_from_env(token_env: &str) -> Option<String> {
    std::env::var(token_env).ok().filter(|t| !t.is_empty())
}

struct AppState {
    view: Arc<dyn EngineView>,
    token: Option<String>,
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

#[allow(clippy::result_large_err)] // the Err is the 401 response itself, built once per request
fn authorized(st: &AppState, headers: &HeaderMap) -> Result<(), Response> {
    let Some(token) = &st.token else { return Ok(()) };
    let header = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).unwrap_or("");
    let (scheme, given) = header.split_once(' ').unwrap_or((header, ""));
    if scheme.eq_ignore_ascii_case("bearer") && ct_eq(given.as_bytes(), token.as_bytes()) {
        Ok(())
    } else {
        Err(detail(StatusCode::UNAUTHORIZED, "missing or bad bearer token"))
    }
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

async fn channels(State(st): St, headers: HeaderMap) -> Response {
    if let Err(r) = authorized(&st, &headers) {
        return r;
    }
    Json(st.view.channels_summary()).into_response()
}

async fn device_get(State(st): St, headers: HeaderMap, Path(path): Path<String>) -> Response {
    if let Err(r) = authorized(&st, &headers) {
        return r;
    }
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

async fn device_post(State(st): St, headers: HeaderMap, Path(path): Path<String>) -> Response {
    let Some(id) = path.strip_suffix("/read-once") else {
        return detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    };
    if let Err(r) = authorized(&st, &headers) {
        return r;
    }
    match st.view.read_once(id).await {
        Ok(v) => Json(v).into_response(),
        Err(ReadOnceError::UnknownDevice) => unknown(id),
        Err(ReadOnceError::Failed(e)) => detail(StatusCode::INTERNAL_SERVER_ERROR, e),
    }
}

async fn reload(State(st): St, headers: HeaderMap) -> Response {
    if let Err(r) = authorized(&st, &headers) {
        return r;
    }
    match st.view.reload().await {
        Ok(v) => Json(v).into_response(),
        Err(e) => detail(StatusCode::BAD_REQUEST, format!("reload failed, old config kept: {e}")),
    }
}

async fn not_found() -> Response {
    detail(StatusCode::NOT_FOUND, "Not Found")
}

pub fn router(view: Arc<dyn EngineView>, token: Option<String>) -> Router {
    let state = Arc::new(AppState { view, token });
    Router::new()
        .route("/health", get(health))
        .route("/metrics", get(metrics))
        .route("/channels", get(channels))
        .route("/devices/{*path}", get(device_get).post(device_post))
        .route("/reload", post(reload))
        .fallback(not_found)
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
        let uri = path.replace(' ', "%20");
        let mut req = Request::builder().method(method).uri(uri);
        if let Some(t) = token {
            req = req.header("Authorization", format!("Bearer {t}"));
        }
        let resp = app.clone().oneshot(req.body(Body::empty()).unwrap()).await.unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::String(String::from_utf8_lossy(&bytes).into())))
    }

    fn app() -> (Router, Arc<FakePoller>) {
        let fake = Arc::new(FakePoller::default());
        (router(fake.clone(), Some("s3cret".into())), fake)
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
        assert!(check_bind("192.0.2.11", None, "OPENCAP_NO_TOKEN").is_err());
        assert!(check_bind("127.0.0.1", None, "X").is_ok());
        assert!(check_bind("localhost", None, "X").is_ok());
        assert!(check_bind("::1", None, "X").is_ok());
        assert!(check_bind("192.0.2.11", Some("t"), "X").is_ok());
    }

    #[tokio::test]
    async fn no_token_on_loopback_is_open() {
        let a = router(Arc::new(FakePoller::default()), None);
        assert_eq!(call(&a, "GET", "/channels", None).await.0, StatusCode::OK);
    }
}
