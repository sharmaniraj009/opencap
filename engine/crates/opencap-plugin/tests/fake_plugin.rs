//! Engine side of `opencap.plugin.v1` against an in-process fake plugin (a tonic server
//! behind a test `Launcher`): planning, the driver contract, restart and re-open after a
//! plugin dies, error mapping, and the channel worker on top. The real Python host is
//! exercised by `tests/python_host.rs` and the Python end-to-end suites.

use std::collections::HashMap;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use indexmap::IndexMap;
use tokio::task::JoinHandle;
use tonic::{Request, Response, Status};

use opencap_core::config::{Channel, DType, Device, PythonPluginCfg, Tag};
use opencap_core::driver::{DriverError, DriverFactory};
use opencap_core::registry::{plan_device, DevicePlan};
use opencap_core::store::TagStore;
use opencap_core::value::{Quality, Value};
use opencap_core::worker::{ChannelWorker, WorkerOptions};
use opencap_plugin::host::{self, LaunchSpec};
use opencap_plugin::proto::driver_error::Kind;
use opencap_plugin::proto::{self, driver_server};
use opencap_plugin::{Launched, Launcher, PluginRegistry};

#[derive(Default)]
struct Fake {
    opened: Mutex<Vec<String>>,
    reads: AtomicUsize,
    down: AtomicBool,
    forget: AtomicBool,
    token: Mutex<Option<String>>,
    /// Launches up to this one are dead: their connections answer UNAVAILABLE, as tonic
    /// reports a peer that is gone.
    killed: AtomicUsize,
}

type Notes = Pin<Box<dyn tokio_stream::Stream<Item = Result<proto::Notification, Status>> + Send>>;

fn err(kind: Kind, message: &str) -> Option<proto::DriverError> {
    Some(proto::DriverError { kind: kind as i32, message: message.into() })
}

#[derive(Clone)]
struct Svc(Arc<Fake>, usize);

impl Svc {
    fn check<T>(&self, req: &Request<T>) -> Result<(), Status> {
        if self.1 <= self.0.killed.load(Ordering::SeqCst) {
            return Err(Status::unavailable("transport error: connection reset"));
        }
        let want = format!("Bearer {}", self.0.token.lock().unwrap().clone().unwrap_or_default());
        match req.metadata().get("authorization").and_then(|v| v.to_str().ok()) {
            Some(got) if got == want => Ok(()),
            _ => Err(Status::unauthenticated("bad token")),
        }
    }

    fn is_open(&self, id: &str) -> bool {
        !self.0.forget.load(Ordering::SeqCst) && self.0.opened.lock().unwrap().iter().any(|d| d == id)
    }
}

fn info() -> Option<proto::DriverInfo> {
    Some(proto::DriverInfo { reports_connection: true, connect_timeout_ms: Some(4_000), stale_by_read_time: false })
}

#[async_trait]
impl driver_server::Driver for Svc {
    async fn hello(&self, req: Request<proto::HelloRequest>) -> Result<Response<proto::HelloReply>, Status> {
        self.check(&req)?;
        Ok(Response::new(proto::HelloReply {
            protocol: host::PROTOCOL.into(),
            host: "fake".into(),
            drivers: vec!["ab_pccc".into(), "modbus_tcp".into()],
        }))
    }

    async fn plan(&self, req: Request<proto::PlanRequest>) -> Result<Response<proto::PlanReply>, Status> {
        self.check(&req)?;
        let req = req.into_inner();
        let device: serde_json::Value = serde_json::from_str(&req.device.unwrap().json).unwrap();
        if device["params"]["broken"] == true {
            return Ok(Response::new(proto::PlanReply {
                error: err(Kind::Other, "ValueError: broken params"),
                ..Default::default()
            }));
        }
        let mut reply = proto::PlanReply { info: info(), ..Default::default() };
        for name in req.tags {
            if name.starts_with("BAD") {
                reply.invalid.insert(name, "unusable address".into());
            } else {
                reply.blocks.push(proto::Block {
                    payload: name.clone().into_bytes().into(),
                    tags: vec![name.clone()],
                    wire_requests: 1,
                    describe: format!("fake {name}"),
                });
            }
        }
        Ok(Response::new(reply))
    }

    async fn open(&self, req: Request<proto::OpenRequest>) -> Result<Response<proto::OpenReply>, Status> {
        self.check(&req)?;
        self.0.forget.store(false, Ordering::SeqCst);
        self.0.opened.lock().unwrap().push(req.into_inner().device_id);
        Ok(Response::new(proto::OpenReply { error: None, info: info() }))
    }

    async fn connect(&self, req: Request<proto::DeviceRef>) -> Result<Response<proto::StatusReply>, Status> {
        self.check(&req)?;
        let id = req.into_inner().device_id;
        if !self.is_open(&id) {
            return Ok(Response::new(proto::StatusReply {
                error: err(Kind::NotOpen, "not open"),
                ..Default::default()
            }));
        }
        if self.0.down.load(Ordering::SeqCst) {
            return Ok(Response::new(proto::StatusReply {
                error: err(Kind::ConnectionLost, "connect failed: refused"),
                ..Default::default()
            }));
        }
        let info = Some(proto::DriverInfo { connect_timeout_ms: Some(1_000), ..info().unwrap() });
        Ok(Response::new(proto::StatusReply { error: None, connected: true, info }))
    }

    async fn read(&self, req: Request<proto::ReadRequest>) -> Result<Response<proto::ReadReply>, Status> {
        self.check(&req)?;
        let req = req.into_inner();
        if !self.is_open(&req.device_id) {
            return Ok(Response::new(proto::ReadReply { error: err(Kind::NotOpen, "not open"), ..Default::default() }));
        }
        let name = String::from_utf8(req.block.to_vec()).unwrap();
        if name == "REJECT" {
            return Ok(Response::new(proto::ReadReply {
                error: err(Kind::BlockConfig, "no such file"),
                connected: true,
                ..Default::default()
            }));
        }
        let n = self.0.reads.fetch_add(1, Ordering::SeqCst) as i64 + 1;
        let tv = proto::TagValue {
            value: Some(proto::Variant { kind: Some(proto::variant::Kind::IntValue(n)) }),
            quality: proto::Quality::Good as i32,
            source_ts_us: Some(1_794_823_200_000_000),
        };
        Ok(Response::new(proto::ReadReply {
            error: None,
            values: HashMap::from([(name, tv)]),
            connected: true,
            driver_us: 5,
        }))
    }

    async fn close(&self, req: Request<proto::DeviceRef>) -> Result<Response<proto::StatusReply>, Status> {
        self.check(&req)?;
        Ok(Response::new(proto::StatusReply { error: None, connected: false, info: info() }))
    }

    async fn identity(&self, req: Request<proto::DeviceRef>) -> Result<Response<proto::IdentityReply>, Status> {
        self.check(&req)?;
        Ok(Response::new(proto::IdentityReply { error: None, json: r#"{"vendor":"fake"}"#.into(), connected: true }))
    }

    async fn browse(&self, req: Request<proto::DeviceRef>) -> Result<Response<proto::BrowseReply>, Status> {
        self.check(&req)?;
        Ok(Response::new(proto::BrowseReply { error: err(Kind::Unsupported, "no browse"), tags: vec![] }))
    }

    type SubscribeStream = Notes;
    async fn subscribe(&self, _: Request<proto::SubscribeRequest>) -> Result<Response<Notes>, Status> {
        Err(Status::unimplemented("no push"))
    }

    async fn health(&self, req: Request<proto::HealthRequest>) -> Result<Response<proto::HealthReply>, Status> {
        self.check(&req)?;
        Ok(Response::new(proto::HealthReply::default()))
    }
}

/// Starts the fake on a free loopback port; `kill()` makes the running one dead.
#[derive(Default)]
struct FakeLauncher {
    fake: Arc<Fake>,
    servers: Mutex<Vec<JoinHandle<()>>>,
    launches: AtomicUsize,
    refuse: AtomicBool,
}

impl FakeLauncher {
    fn kill(&self) {
        self.fake.killed.store(self.launches.load(Ordering::SeqCst), Ordering::SeqCst);
    }
}

impl Drop for FakeLauncher {
    fn drop(&mut self) {
        for s in self.servers.lock().unwrap().drain(..) {
            s.abort();
        }
    }
}

#[async_trait]
impl Launcher for FakeLauncher {
    async fn launch(&self, spec: &LaunchSpec) -> Result<Launched, String> {
        if self.refuse.load(Ordering::SeqCst) {
            return Err("python3: No such file or directory".into());
        }
        let epoch = self.launches.fetch_add(1, Ordering::SeqCst) + 1;
        *self.fake.token.lock().unwrap() = Some(spec.token.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let svc = driver_server::DriverServer::new(Svc(self.fake.clone(), epoch));
        let server = tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_service(svc)
                .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
                .await;
        });
        self.servers.lock().unwrap().push(server);
        let mut client = host::client(host::connect(&addr.to_string()).await?, &spec.token);
        host::hello(&mut client, &spec.drivers).await?;
        Ok(Launched { client, process: None })
    }
}

fn device(id: &str, tags: &[&str]) -> Device {
    let (channel, name) = id.split_once('/').unwrap();
    let mut d = Device::new(
        id,
        channel,
        name,
        "ab_pccc",
        "192.0.2.1",
        44818,
        tags.iter().map(|n| Tag::new(n, "N7:0", DType::Int16)).collect(),
    );
    d.plugin = Some("python".into());
    d.scan_ms = 50;
    d.min_scan_ms = 1;
    d.timeout_ms = 300;
    d.retries = 0;
    d.demote_after = 3;
    d.demote_for_ms = 300;
    d
}

fn channel(devices: Vec<Device>) -> Channel {
    Channel { name: devices[0].channel.clone(), enabled: true, devices }
}

fn base() -> DriverFactory {
    Arc::new(|d: &Device| Err(format!("driver {} is not compiled in", d.driver)))
}

async fn setup(devices: Vec<Device>) -> (Arc<FakeLauncher>, Arc<PluginRegistry>, DriverFactory, Vec<Channel>) {
    let launcher = Arc::new(FakeLauncher::default());
    let reg = PluginRegistry::with_launcher(launcher.clone());
    let channels = vec![channel(devices)];
    reg.prepare(&PythonPluginCfg::default_for_tests(), &channels).await;
    let factory = reg.factory(base());
    (launcher, reg, factory, channels)
}

trait TestCfg {
    fn default_for_tests() -> Self;
}

impl TestCfg for PythonPluginCfg {
    fn default_for_tests() -> Self {
        serde_json::from_str("{}").unwrap()
    }
}

#[tokio::test]
async fn plans_come_from_the_plugin_and_reads_go_through_it() {
    let d = device("S/D", &["A", "B", "BAD1"]);
    let (launcher, reg, factory, _) = setup(vec![d.clone()]).await;
    let plan = plan_device(&d, &factory);
    assert!(plan.ok(), "{:?}", plan.error);
    assert_eq!(plan.blocks(), 2);
    assert_eq!(plan.invalid().keys().collect::<Vec<_>>(), vec!["BAD1"]);
    assert_eq!(plan.groups[0].plan.blocks[0].describe(), "fake A");

    let mut drv = factory(&d).unwrap();
    assert_eq!(drv.name(), "ab_pccc");
    assert!(drv.reports_connection());
    assert_eq!(drv.connect_timeout(), Some(Duration::from_secs(4)), "from Plan");
    drv.connect().await.unwrap();
    assert!(drv.connected());
    assert_eq!(drv.connect_timeout(), Some(Duration::from_secs(1)), "updated by Connect");
    let block = plan.groups[0].plan.blocks[0].clone();
    let values = drv.read(block.as_ref()).await.unwrap();
    assert_eq!(values["A"].value, Some(Value::Int(1)));
    assert_eq!(values["A"].quality, Quality::Good);
    assert_eq!(drv.identity().await["vendor"], "fake");
    drv.close().await;
    assert!(!drv.connected());
    assert_eq!(launcher.launches.load(Ordering::SeqCst), 1, "one process per channel");
    assert_eq!(*launcher.fake.opened.lock().unwrap(), vec!["S/D"]);
    reg.shutdown().await;
}

#[tokio::test]
async fn a_dead_plugin_is_connection_lost_and_the_device_is_reopened_after_the_restart() {
    let d = device("S/D", &["A"]);
    let (launcher, reg, factory, _) = setup(vec![d.clone()]).await;
    let block = plan_device(&d, &factory).groups[0].plan.blocks[0].clone();
    let mut drv = factory(&d).unwrap();
    drv.connect().await.unwrap();
    drv.read(block.as_ref()).await.unwrap();

    launcher.kill();
    let e = drv.read(block.as_ref()).await.unwrap_err();
    assert!(matches!(e, DriverError::ConnectionLost(_)), "{e:?}");
    assert!(!drv.connected());
    let e = drv.read(block.as_ref()).await.unwrap_err();
    assert!(e.to_string().contains("restarted or not running"), "{e}");
    // Restart backoff: the first attempt right after the death is refused.
    let e = drv.connect().await.unwrap_err();
    assert!(matches!(&e, DriverError::ConnectionLost(m) if m.contains("restarts in")), "{e:?}");
    tokio::time::sleep(host::RESTART_MIN + Duration::from_millis(100)).await;
    drv.connect().await.unwrap();
    assert_eq!(launcher.launches.load(Ordering::SeqCst), 2);
    assert_eq!(launcher.fake.opened.lock().unwrap().len(), 2, "opened again in the new process");
    assert!(drv.read(block.as_ref()).await.is_ok());
    reg.shutdown().await;
}

#[tokio::test]
async fn driver_errors_keep_their_class_and_a_forgotten_device_is_reopened_once() {
    let d = device("S/D", &["A", "REJECT"]);
    let (launcher, reg, factory, _) = setup(vec![d.clone()]).await;
    let plan = plan_device(&d, &factory);
    let mut drv = factory(&d).unwrap();
    launcher.fake.down.store(true, Ordering::SeqCst);
    let e = drv.connect().await.unwrap_err();
    assert_eq!(e, DriverError::ConnectionLost("connect failed: refused".into()));
    launcher.fake.down.store(false, Ordering::SeqCst);
    launcher.fake.forget.store(true, Ordering::SeqCst); // the plugin lost its devices
    drv.connect().await.unwrap();
    assert_eq!(launcher.fake.opened.lock().unwrap().len(), 2, "re-opened once");
    let reject = plan.groups[0].plan.blocks.iter().find(|b| b.tags() == ["REJECT"]).unwrap().clone();
    assert_eq!(drv.read(reject.as_ref()).await.unwrap_err(), DriverError::BlockConfig("no such file".into()));
    reg.shutdown().await;
}

#[tokio::test]
async fn devices_that_cannot_be_planned_are_listed_not_polled() {
    let mut broken = device("S/BROKEN", &["A"]);
    broken.params.insert("broken".into(), true.into());
    let ok = device("S/OK", &["A"]);
    let mut native = device("S/NATIVE", &["A"]);
    native.plugin = None;
    let (_, reg, factory, _) = setup(vec![broken.clone(), ok.clone(), native.clone()]).await;
    assert_eq!(plan_device(&broken, &factory).error.as_deref(), Some("python plugin: ValueError: broken params"));
    assert!(plan_device(&ok, &factory).ok());
    assert_eq!(
        plan_device(&native, &factory).error.as_deref(),
        Some("driver ab_pccc is not compiled in"),
        "not routed"
    );
    let mut changed = ok.clone();
    changed.port = 2;
    assert!(factory(&changed).err().unwrap().contains("not prepared"), "a config change needs prepare()");
    reg.shutdown().await;

    let launcher = Arc::new(FakeLauncher::default());
    launcher.refuse.store(true, Ordering::SeqCst);
    let reg = PluginRegistry::with_launcher(launcher.clone());
    reg.prepare(&PythonPluginCfg::default_for_tests(), &[channel(vec![ok.clone()])]).await;
    let e = plan_device(&ok, &reg.factory(base())).error.unwrap();
    assert!(e.contains("could not be started: python3: No such file"), "{e}");
    reg.shutdown().await;
}

#[tokio::test]
async fn prepare_replans_changes_and_stops_hosts_no_channel_needs() {
    let a = device("A/D", &["X"]);
    let b = device("B/D", &["X"]);
    let launcher = Arc::new(FakeLauncher::default());
    let reg = PluginRegistry::with_launcher(launcher.clone());
    let cfg = PythonPluginCfg::default_for_tests();
    reg.prepare(&cfg, &[channel(vec![a.clone()]), channel(vec![b.clone()])]).await;
    assert_eq!(launcher.launches.load(Ordering::SeqCst), 2, "one host per channel");
    reg.prepare(&cfg, &[channel(vec![a.clone()]), channel(vec![b.clone()])]).await;
    assert_eq!(launcher.launches.load(Ordering::SeqCst), 2, "nothing changed: nothing restarted");
    assert!(reg.host("B").is_some());
    reg.prepare(&cfg, &[channel(vec![a.clone()])]).await;
    assert!(reg.host("B").is_none(), "channel B no longer routes anything");
    let mut a2 = a.clone();
    a2.tags.push(Tag::new("Y", "N7:1", DType::Int16));
    reg.prepare(&cfg, &[channel(vec![a2.clone()])]).await;
    assert_eq!(plan_device(&a2, &reg.factory(base())).blocks(), 2, "re-planned");
    reg.shutdown().await;
    assert!(reg.host("A").is_none());
}

/// The channel worker on top: a plugin killed mid-run turns the device bad_comm (last value
/// kept), and the device recovers by itself once the plugin is back.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn worker_goes_bad_comm_when_the_plugin_dies_and_recovers() {
    let d = device("S/D", &["A"]);
    let (launcher, reg, factory, channels) = setup(vec![d.clone()]).await;
    let plans: IndexMap<String, Arc<DevicePlan>> = [(d.id.clone(), Arc::new(plan_device(&d, &factory)))].into();
    let store = Arc::new(TagStore::new());
    let opts = WorkerOptions {
        start_jitter: 0.0,
        backoff_base_s: 0.05,
        backoff_max_s: 0.2,
        stats_interval_s: 0.0,
        ..Default::default()
    };
    let worker = ChannelWorker::new(&channels[0], &plans, store.clone(), factory, opts);
    worker.start();
    let wait = |what: &'static str, pred: &dyn Fn() -> bool| {
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            if pred() {
                return;
            }
            assert!(std::time::Instant::now() < deadline, "timed out waiting for {what}");
            std::thread::sleep(Duration::from_millis(20));
        }
    };
    let quality = || store.get("S/D", "A").map(|t| t.quality);
    wait("good", &|| quality() == Some(Quality::Good));
    launcher.kill();
    wait("bad_comm", &|| quality() == Some(Quality::BadComm));
    assert!(store.get("S/D", "A").unwrap().value.is_some(), "last value kept");
    wait("good again", &|| quality() == Some(Quality::Good));
    assert_eq!(launcher.launches.load(Ordering::SeqCst), 2);
    worker.stop().await;
    reg.shutdown().await;
}
