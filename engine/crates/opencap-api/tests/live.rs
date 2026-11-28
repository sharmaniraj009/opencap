//! The live feed (`/api/v1/ws`) over a real socket: auth, origin, snapshots, conflated changes,
//! tag filters, devices that are not polled, resync of a slow reader (#164).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use opencap_api::live::LiveCfg;
use opencap_api::{ApiOptions, Auth, EngineView, ReadOnceError};
use serde_json::{json, Value};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{HeaderName, HeaderValue};
use tokio_tungstenite::tungstenite::Message;

#[derive(Default)]
struct Fake {
    devices: Mutex<HashMap<String, (u64, Value)>>,
}

impl Fake {
    fn set(&self, dev: &str, values: Value) {
        let mut d = self.devices.lock().unwrap();
        let v = d.get(dev).map_or(0, |(v, _)| *v) + 1;
        d.insert(dev.into(), (v, values));
    }
    fn drop_device(&self, dev: &str) {
        self.devices.lock().unwrap().remove(dev);
    }
}

#[async_trait]
impl EngineView for Fake {
    fn health(&self) -> Value {
        json!({"status": "ok"})
    }
    fn channels_summary(&self) -> Value {
        json!([])
    }
    fn device_detail(&self, _: &str) -> Option<Value> {
        None
    }
    fn device_values(&self, d: &str) -> Option<Value> {
        self.devices.lock().unwrap().get(d).map(|(_, v)| v.clone())
    }
    async fn read_once(&self, _: &str) -> Result<Value, ReadOnceError> {
        Err(ReadOnceError::UnknownDevice)
    }
    async fn reload(&self) -> Result<Value, String> {
        Ok(json!({}))
    }
    fn metrics(&self) -> String {
        String::new()
    }
    fn readiness(&self) -> Value {
        json!({"ready": true, "waiting": []})
    }
    fn about(&self) -> Value {
        json!({})
    }
    fn support(&self) -> Value {
        json!({})
    }
    fn live_version(&self, d: &str) -> Option<u64> {
        self.devices.lock().unwrap().get(d).map(|(v, _)| *v)
    }
}

fn tv(v: i64) -> Value {
    json!({"value": v, "quality": "good", "source_ts": "2026-01-01T00:00:00Z"})
}

fn open() -> Auth {
    Auth::new(None, &[], |_| None).unwrap()
}

async fn start(fake: Arc<Fake>, auth: Auth, cors: &[&str], live: LiveCfg) -> std::net::SocketAddr {
    let app = opencap_api::router_with_options(
        fake,
        auth,
        ApiOptions { cors_origins: cors.iter().map(|s| s.to_string()).collect(), live, ..Default::default() },
    );
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(l, app).await;
    });
    addr
}

fn quick() -> LiveCfg {
    LiveCfg { conflate: Duration::from_millis(60), tick: Duration::from_millis(10), ..LiveCfg::default() }
}

type Ws = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn connect(addr: std::net::SocketAddr, protocols: &str, extra: &[(&str, &str)]) -> Result<Ws, u16> {
    let mut req = format!("ws://{addr}/api/v1/ws").into_client_request().unwrap();
    req.headers_mut().insert("Sec-WebSocket-Protocol", HeaderValue::from_str(protocols).unwrap());
    for (k, v) in extra {
        req.headers_mut().insert(HeaderName::from_bytes(k.as_bytes()).unwrap(), HeaderValue::from_str(v).unwrap());
    }
    match tokio_tungstenite::connect_async(req).await {
        Ok((ws, _)) => Ok(ws),
        Err(tokio_tungstenite::tungstenite::Error::Http(r)) => Err(r.status().as_u16()),
        Err(e) => panic!("{e}"),
    }
}

async fn next(ws: &mut Ws) -> Value {
    // generous bound: only a hung feed trips it
    let m = tokio::time::timeout(Duration::from_secs(10), ws.next()).await.expect("no message").unwrap().unwrap();
    serde_json::from_str(m.to_text().unwrap()).unwrap()
}

async fn sub(ws: &mut Ws, v: Value) {
    ws.send(Message::Text(v.to_string().into())).await.unwrap();
}

fn names(m: &Value) -> Vec<String> {
    let mut n: Vec<String> =
        m["changes"].as_array().unwrap().iter().map(|c| c["tag"].as_str().unwrap().into()).collect();
    n.sort();
    n
}

const TOK: &str = "opencap.v1, opencap.bearer.dG9r"; // the token "tok"

#[tokio::test]
async fn subscribe_snapshot_then_only_changes() {
    let fake = Arc::new(Fake::default());
    fake.set("S/D", json!({"a": tv(1), "b": tv(2)}));
    let addr = start(fake.clone(), Auth::single("tok"), &[], quick()).await;
    let mut ws = connect(addr, TOK, &[]).await.unwrap();
    sub(&mut ws, json!({"subscribe": {"devices": ["S/D"]}})).await;
    let ack = next(&mut ws).await;
    assert_eq!(ack["type"], "subscribed");
    assert_eq!(ack["devices"], json!(["S/D"]));
    assert_eq!(ack["conflate_ms"], 60);
    let snap = next(&mut ws).await;
    assert_eq!((snap["type"].as_str(), names(&snap)), (Some("snapshot"), vec!["a".into(), "b".into()]));
    assert_eq!(snap["changes"][0]["quality"], "good");

    fake.set("S/D", json!({"a": tv(1), "b": tv(3)}));
    let ch = next(&mut ws).await;
    assert_eq!((ch["type"].as_str(), names(&ch)), (Some("changes"), vec!["b".into()]));
    assert_eq!(ch["changes"][0]["value"], 3);
}

#[tokio::test]
async fn tag_filter_only_sends_the_tags_asked_for() {
    let fake = Arc::new(Fake::default());
    fake.set("S/D", json!({"a": tv(1), "b": tv(2)}));
    let addr = start(fake.clone(), Auth::single("tok"), &[], quick()).await;
    let mut ws = connect(addr, TOK, &[]).await.unwrap();
    sub(&mut ws, json!({"subscribe": {"tags": [{"device": "S/D", "tag": "a"}, {"device": "S/D", "tag": "nope"}]}}))
        .await;
    assert_eq!(next(&mut ws).await["type"], "subscribed");
    let snap = next(&mut ws).await;
    assert_eq!(names(&snap), vec!["a"]);
    assert_eq!(snap["missing"], json!(["nope"]));
    fake.set("S/D", json!({"a": tv(1), "b": tv(9)})); // b is not ours: nothing arrives
    fake.set("S/D", json!({"a": tv(5), "b": tv(9)}));
    let ch = next(&mut ws).await;
    assert_eq!((ch["type"].as_str(), names(&ch)), (Some("changes"), vec!["a".into()]));
}

#[tokio::test]
async fn a_device_that_is_not_polled_is_unavailable_and_recovers() {
    let fake = Arc::new(Fake::default());
    let addr = start(fake.clone(), Auth::single("tok"), &[], quick()).await;
    let mut ws = connect(addr, TOK, &[]).await.unwrap();
    sub(&mut ws, json!({"subscribe": {"devices": ["S/GHOST"]}})).await;
    assert_eq!(next(&mut ws).await["type"], "subscribed");
    assert_eq!(next(&mut ws).await["type"], "unavailable");
    fake.set("S/GHOST", json!({"a": tv(1)}));
    let snap = next(&mut ws).await;
    assert_eq!((snap["type"].as_str(), names(&snap)), (Some("snapshot"), vec!["a".into()]));
    fake.drop_device("S/GHOST");
    assert_eq!(next(&mut ws).await["type"], "unavailable");
}

#[tokio::test]
async fn bad_messages_get_an_error_and_the_connection_stays() {
    let fake = Arc::new(Fake::default());
    let addr = start(fake, Auth::single("tok"), &[], quick()).await;
    let mut ws = connect(addr, TOK, &[]).await.unwrap();
    ws.send(Message::Text("not json".into())).await.unwrap();
    let e = next(&mut ws).await;
    assert_eq!(e["type"], "error");
    assert!(e["detail"].as_str().unwrap().contains("subscribe"), "{e}");
    sub(&mut ws, json!({"subscribe": {"devices": []}})).await;
    assert_eq!(next(&mut ws).await["type"], "subscribed");
}

#[tokio::test]
async fn auth_and_origin_are_checked_before_the_upgrade() {
    let fake = Arc::new(Fake::default());
    let addr = start(fake, Auth::single("tok"), &["https://app.example"], quick()).await;
    assert_eq!(connect(addr, "opencap.v1", &[]).await.err(), Some(401));
    assert_eq!(connect(addr, "opencap.v1, opencap.bearer.d3Jvbmc", &[]).await.err(), Some(401)); // "wrong"
    assert_eq!(connect(addr, "opencap.v1, opencap.bearer.%%%", &[]).await.err(), Some(401));
    assert!(connect(addr, "opencap.v1", &[("Authorization", "Bearer tok")]).await.is_ok());
    let own = format!("http://{addr}");
    assert!(connect(addr, TOK, &[("Origin", "https://app.example")]).await.is_ok());
    assert!(connect(addr, TOK, &[("Origin", own.as_str())]).await.is_ok());
    assert_eq!(connect(addr, TOK, &[("Origin", "https://evil.example")]).await.err(), Some(403));
}

#[tokio::test]
async fn open_mode_answers_loopback_only() {
    let fake = Arc::new(Fake::default());
    let addr = start(fake, open(), &[], quick()).await;
    assert!(connect(addr, "opencap.v1", &[]).await.is_ok());
    assert_eq!(connect(addr, "opencap.v1", &[("Host", "plc.example")]).await.err(), Some(403));
}

#[tokio::test]
async fn the_connection_cap_refuses_with_503() {
    let fake = Arc::new(Fake::default());
    let cfg = LiveCfg { max_connections: 1, ..quick() };
    let addr = start(fake, Auth::single("tok"), &[], cfg).await;
    let h = [("Authorization", "Bearer tok")];
    let _first = connect(addr, "opencap.v1", &h).await.unwrap();
    // the first connection counts once its task has attached; allow that a moment
    let mut got = None;
    for _ in 0..100 {
        match connect(addr, "opencap.v1", &h).await {
            Err(c) => {
                got = Some(c);
                break;
            }
            Ok(_) => tokio::time::sleep(Duration::from_millis(20)).await,
        }
    }
    assert_eq!(got, Some(503));
}

#[tokio::test]
async fn a_reader_that_falls_behind_ends_on_the_newest_value() {
    let fake = Arc::new(Fake::default());
    fake.set("S/D", json!({"a": tv(0)}));
    let cfg = LiveCfg { conflate: Duration::ZERO, max_queue_bytes: 200, ..quick() };
    let addr = start(fake.clone(), Auth::single("tok"), &[], cfg).await;
    let mut ws = connect(addr, TOK, &[]).await.unwrap();
    sub(&mut ws, json!({"subscribe": {"devices": ["S/D"]}})).await;
    assert_eq!(next(&mut ws).await["type"], "subscribed");
    assert_eq!(next(&mut ws).await["type"], "snapshot");
    for i in 1..=50 {
        fake.set("S/D", json!({"a": tv(i)}));
        tokio::time::sleep(Duration::from_millis(15)).await;
    }
    let mut last = 0;
    while last != 50 {
        let m = next(&mut ws).await;
        if let Some(c) = m["changes"].as_array().and_then(|c| c.first()) {
            last = c["value"].as_i64().unwrap();
        }
    }
}
