//! `api.request_refused` and `api.bind_failed` (D-079, #222): the fields, the one-line-a-second
//! limit per client and status, no secret in any line, and the bind-failure reasons.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use opencap_api::{bind_api, router_with, Auth, EngineView, ReadOnceError, RefusalLog, Role, TokenSpec};
use serde_json::{json, Value};
use tower::ServiceExt;
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::layer::{Context, SubscriberExt};
use tracing_subscriber::Layer;

struct Stub;

#[async_trait]
impl EngineView for Stub {
    fn health(&self) -> Value {
        json!({"status": "ok"})
    }
    fn channels_summary(&self) -> Value {
        json!([])
    }
    fn device_detail(&self, _: &str) -> Option<Value> {
        None
    }
    fn device_values(&self, _: &str) -> Option<Value> {
        None
    }
    async fn read_once(&self, _: &str) -> Result<Value, ReadOnceError> {
        Err(ReadOnceError::UnknownDevice)
    }
    async fn reload(&self) -> Result<Value, String> {
        Ok(json!({}))
    }
    async fn reload_by(&self, who: &str, via: &str) -> Result<Value, String> {
        Ok(json!({"who": who, "via": via}))
    }
    fn metrics(&self) -> String {
        String::new()
    }
    fn readiness(&self) -> Value {
        json!({"ready": true})
    }
    fn about(&self) -> Value {
        json!({})
    }
    fn support(&self) -> Value {
        json!({})
    }
}

/// One captured event: its level and fields (`message` included).
type Line = (Level, HashMap<String, String>);

#[derive(Clone, Default)]
struct Capture(Arc<Mutex<Vec<Line>>>);

struct Fields<'a>(&'a mut HashMap<String, String>);

impl Visit for Fields<'_> {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.0.insert(field.name().to_string(), value.to_string());
    }

    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        self.0.insert(field.name().to_string(), format!("{value:?}"));
    }
}

impl<S: Subscriber> Layer<S> for Capture {
    fn on_event(&self, event: &Event<'_>, _: Context<'_, S>) {
        let mut fields = HashMap::new();
        event.record(&mut Fields(&mut fields));
        self.0.lock().unwrap().push((*event.metadata().level(), fields));
    }
}

impl Capture {
    fn named(&self, name: &str) -> Vec<Line> {
        self.0.lock().unwrap().iter().filter(|(_, f)| f.get("message").is_some_and(|m| m == name)).cloned().collect()
    }
    fn text(&self) -> String {
        format!("{:?}", self.0.lock().unwrap())
    }
}

fn app(clock: Arc<Mutex<f64>>) -> axum::Router {
    let specs = [
        TokenSpec { name: "grafana".into(), role: Role::Viewer, token_env: "V".into() },
        TokenSpec { name: "alice".into(), role: Role::Admin, token_env: "A".into() },
    ];
    let env = |k: &str| match k {
        "V" => Some("viewer-secret-value".to_string()),
        "A" => Some("admin-secret-value".to_string()),
        _ => None,
    };
    let auth = Auth::new(None, &specs, env).unwrap();
    router_with(Arc::new(Stub), auth, RefusalLog::new(1.0, Arc::new(move || *clock.lock().unwrap())))
}

async fn send(app: &axum::Router, method: &str, path: &str, token: Option<&str>, extra: &[(&str, &str)]) -> StatusCode {
    let mut req = Request::builder().method(method).uri(path).header("Host", "127.0.0.1");
    if let Some(t) = token {
        req = req.header("Authorization", format!("Bearer {t}"));
    }
    for (k, v) in extra {
        req = req.header(*k, *v);
    }
    app.clone().oneshot(req.body(Body::empty()).unwrap()).await.unwrap().status()
}

#[tokio::test]
async fn refusals_are_logged_once_each_with_who_and_no_secret() {
    let capture = Capture::default();
    let _guard = tracing::subscriber::set_default(tracing_subscriber::registry().with(capture.clone()));
    let clock = Arc::new(Mutex::new(10.0));
    let a = app(clock.clone());
    assert_eq!(send(&a, "GET", "/channels", Some("wrong-token-value"), &[]).await, StatusCode::UNAUTHORIZED);
    assert_eq!(
        send(&a, "POST", "/reload", Some("viewer-secret-value"), &[("x-opencap-client", "ui")]).await,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        send(&a, "POST", "/reload", Some("admin-secret-value"), &[("content-length", "2097152")]).await,
        StatusCode::PAYLOAD_TOO_LARGE
    );
    assert_eq!(send(&a, "POST", "/reload", Some("admin-secret-value"), &[]).await, StatusCode::OK, "not refused");

    let lines = capture.named("api.request_refused");
    let by_status: HashMap<String, &Line> = lines.iter().map(|l| (l.1["status"].clone(), l)).collect();
    assert_eq!(lines.len(), 3, "{lines:?}");
    let (l401, l403, l413) = (by_status["401"], by_status["403"], by_status["413"]);
    assert_eq!((l401.0, l403.0, l413.0), (Level::WARN, Level::WARN, Level::INFO));
    assert_eq!((l401.1["who"].as_str(), l401.1["via"].as_str()), ("unknown", "api"));
    assert_eq!((l403.1["who"].as_str(), l403.1["via"].as_str()), ("grafana", "ui"));
    assert_eq!(l413.1["who"], "alice");
    for (_, f) in &lines {
        for key in ["method", "path", "status", "who", "client", "via"] {
            assert!(f.contains_key(key), "{key} missing in {f:?}");
        }
        assert!(!f.contains_key("suppressed"));
    }
    assert_eq!(l403.1["method"], "POST");
    assert_eq!(l403.1["path"], "/reload");
    let text = capture.text();
    for secret in ["wrong-token-value", "viewer-secret-value", "admin-secret-value"] {
        assert!(!text.contains(secret), "{secret} reached a log line");
    }
}

#[tokio::test]
async fn a_flood_of_bad_tokens_costs_one_line_a_second() {
    let capture = Capture::default();
    let _guard = tracing::subscriber::set_default(tracing_subscriber::registry().with(capture.clone()));
    let clock = Arc::new(Mutex::new(50.0));
    let a = app(clock.clone());
    for _ in 0..6 {
        assert_eq!(send(&a, "GET", "/channels", Some("nope"), &[]).await, StatusCode::UNAUTHORIZED);
    }
    assert_eq!(capture.named("api.request_refused").len(), 1);
    *clock.lock().unwrap() = 51.5;
    send(&a, "GET", "/channels", Some("nope"), &[]).await;
    let lines = capture.named("api.request_refused");
    assert_eq!(lines.len(), 2);
    assert_eq!(lines[1].1["suppressed"], "5", "the five dropped lines are counted on the next one");
}

#[tokio::test]
async fn the_reload_handler_knows_who_asked() {
    let a = app(Arc::new(Mutex::new(0.0)));
    let req = Request::builder()
        .method("POST")
        .uri("/reload")
        .header("Host", "127.0.0.1")
        .header("Authorization", "Bearer admin-secret-value")
        .header("x-opencap-client", "ui")
        .body(Body::empty())
        .unwrap();
    let resp = a.oneshot(req).await.unwrap();
    let body: Value = serde_json::from_slice(&axum::body::to_bytes(resp.into_body(), 1 << 16).await.unwrap()).unwrap();
    assert_eq!(body, json!({"who": "alice", "via": "ui"}));
}

#[tokio::test]
async fn bind_failures_name_their_reason() {
    let taken = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let port = taken.local_addr().unwrap().port();
    let e = bind_api("127.0.0.1", port).await.unwrap_err();
    assert_eq!(e.reason, "in_use");
    assert!(e.message.starts_with(&format!("cannot bind 127.0.0.1:{port}")), "{}", e.message);
    // an address this host does not have
    assert_eq!(bind_api("192.0.2.77", 0).await.unwrap_err().reason, "address_unavailable");
    assert_eq!(bind_api("not an address!", 0).await.unwrap_err().reason, "bad_address");
    assert!(bind_api("127.0.0.1", 0).await.is_ok());
}
