//! Plugin process supervision (`proto/README.md`, "Launching a plugin process").
//!
//! A [`PluginHost`] owns at most one plugin process at a time:
//! - [`PluginHost::ensure`] starts it when none runs (command + `--socket`/`--listen`,
//!   `--driver`, `--exit-on-stdin-eof`, `OPENCAP_PLUGIN_TOKEN`), waits for its ready line,
//!   connects and checks `Hello`. Each start is a new *generation*: a device opened in an
//!   earlier generation must be opened again.
//! - A process that exits, or a call that fails in transport ([`PluginHost::failed`]), ends
//!   the generation. The next start waits 1 s, then 2 s, 4 s ... 60 s after repeated deaths
//!   (reset once a process has lived 60 s), so a crashing plugin cannot spin.
//! - [`PluginHost::shutdown`] closes the process's stdin (the host closes its drivers and
//!   exits) and kills it if it is still alive after a grace period. `kill_on_drop` and the
//!   stdin pipe make sure no plugin outlives the engine.
//!
//! The process's stderr (its log) and any extra stdout lines are copied into the engine log
//! as `plugin.stderr` / `plugin.stdout`.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value as Json;
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{ChildStdin, Command};
use tokio::sync::{oneshot, watch, Mutex};
use tokio::time::Instant;
use tonic::metadata::{Ascii, MetadataValue};
use tonic::service::interceptor::InterceptedService;
use tonic::service::Interceptor;
use tonic::transport::{Channel, Endpoint};
use tonic::{Request, Status};
use tracing::{debug, error, info, warn};

use crate::proto;
use crate::proto::driver_client::DriverClient;

pub const PROTOCOL: &str = "opencap.plugin.v1";
pub const READY: &str = "opencap-plugin-ready";
pub const TOKEN_ENV: &str = "OPENCAP_PLUGIN_TOKEN";
pub const RESTART_MIN: Duration = Duration::from_secs(1);
pub const RESTART_MAX: Duration = Duration::from_secs(60);
/// A process that lived this long resets the restart backoff.
pub const STABLE_AFTER: Duration = Duration::from_secs(60);
pub const STOP_GRACE: Duration = Duration::from_secs(3);
pub const MAX_MESSAGE: usize = 64 * 1024 * 1024;
/// Generations are unique across hosts, so a device opened in a replaced host's process is
/// never mistaken for open in its successor's.
static NEXT_GENERATION: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
const STDERR_TAIL: usize = 20;

pub type Client = DriverClient<InterceptedService<Channel, Auth>>;

/// Adds `authorization: Bearer <token>` to every call.
#[derive(Clone)]
pub struct Auth(MetadataValue<Ascii>);

impl Auth {
    pub fn new(token: &str) -> Self {
        Self(format!("Bearer {token}").parse().expect("hex token is valid metadata"))
    }
}

impl Interceptor for Auth {
    fn call(&mut self, mut req: Request<()>) -> Result<Request<()>, Status> {
        req.metadata_mut().insert("authorization", self.0.clone());
        Ok(req)
    }
}

pub fn client(channel: Channel, token: &str) -> Client {
    DriverClient::with_interceptor(channel, Auth::new(token))
        .max_decoding_message_size(MAX_MESSAGE)
        .max_encoding_message_size(MAX_MESSAGE)
}

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum PluginError {
    #[error("could not be started: {0}")]
    Start(String),
    #[error("restarts in {0:.1} s")]
    Backoff(f64),
    #[error("is stopped")]
    Stopped,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transport {
    Unix,
    Tcp,
}

impl Transport {
    /// `plugins.python.transport`: `auto` is a Unix socket where there are Unix sockets.
    pub fn from_config(name: &str) -> Self {
        match name {
            "tcp" => Transport::Tcp,
            "unix" if cfg!(unix) => Transport::Unix,
            "unix" => {
                warn!("plugins.python.transport: unix is not available on this platform; using tcp");
                Transport::Tcp
            }
            _ if cfg!(unix) => Transport::Unix,
            _ => Transport::Tcp,
        }
    }
}

/// How to start one plugin process.
#[derive(Debug, Clone)]
pub struct LaunchSpec {
    /// For logs and errors: the channel the process serves.
    pub label: String,
    pub command: Vec<String>,
    pub drivers: Vec<String>,
    pub transport: Transport,
    pub start_timeout: Duration,
    pub token: String,
}

/// A started plugin: its client and, for a real process, the process.
pub struct Launched {
    pub client: Client,
    pub process: Option<Process>,
}

/// Starts plugins. [`ProcessLauncher`] in production; tests substitute an in-process server.
#[async_trait]
pub trait Launcher: Send + Sync {
    async fn launch(&self, spec: &LaunchSpec) -> Result<Launched, String>;
}

/// A running plugin process. Dropping it kills the process.
pub struct Process {
    pub pid: Option<u32>,
    exited: watch::Receiver<Option<String>>,
    kill: Option<oneshot::Sender<()>>,
    stdin: Option<ChildStdin>,
    _dir: Option<PrivateDir>,
}

impl Process {
    /// The exit status once the process has exited.
    pub fn exited(&self) -> Option<String> {
        self.exited.borrow().clone()
    }

    /// Close stdin so the plugin stops cleanly; kill it after `grace`.
    pub async fn stop(mut self, grace: Duration) {
        self.stdin.take();
        let mut exited = self.exited.clone();
        let _ = tokio::time::timeout(grace, exited.wait_for(Option::is_some)).await;
        // Drop sends the kill if it is still running.
    }
}

impl Drop for Process {
    fn drop(&mut self) {
        if let Some(kill) = self.kill.take() {
            let _ = kill.send(());
        }
    }
}

/// A directory only this user can enter, for the Unix socket; removed on drop.
struct PrivateDir(PathBuf);

impl PrivateDir {
    fn create() -> std::io::Result<Self> {
        let name = format!("opencap-plugin-{}-{:08x}", std::process::id(), rand::random::<u32>());
        let path = std::env::temp_dir().join(name);
        let mut builder = std::fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        builder.create(&path)?;
        Ok(Self(path))
    }
}

impl Drop for PrivateDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

type Tail = Arc<StdMutex<VecDeque<String>>>;

fn tail_text(tail: &Tail) -> String {
    let lines = tail.lock().unwrap_or_else(|e| e.into_inner());
    if lines.is_empty() {
        String::new()
    } else {
        format!("; stderr: {}", lines.iter().cloned().collect::<Vec<_>>().join(" | "))
    }
}

/// Copy a pipe's lines into the engine log; keep the last lines of stderr for error texts.
fn forward<R: AsyncRead + Unpin + Send + 'static>(pipe: R, label: String, stream: &'static str, tail: Option<Tail>) {
    tokio::spawn(async move {
        let mut lines = BufReader::new(pipe).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(tail) = &tail {
                let mut t = tail.lock().unwrap_or_else(|e| e.into_inner());
                if t.len() == STDERR_TAIL {
                    t.pop_front();
                }
                t.push_back(line.clone());
            }
            let level = serde_json::from_str::<Json>(&line)
                .ok()
                .and_then(|j| j.get("level").and_then(Json::as_str).map(str::to_ascii_lowercase));
            match level.as_deref() {
                Some("error") | Some("critical") => error!(plugin = %label, stream, line = %line, "plugin.log"),
                Some("warning") | Some("warn") => warn!(plugin = %label, stream, line = %line, "plugin.log"),
                Some("debug") => debug!(plugin = %label, stream, line = %line, "plugin.log"),
                _ => info!(plugin = %label, stream, line = %line, "plugin.log"),
            }
        }
    });
}

/// Connect to a plugin's address from its ready line: `unix:/path` or `host:port`.
pub async fn connect(address: &str) -> Result<Channel, String> {
    if let Some(path) = address.strip_prefix("unix:") {
        #[cfg(unix)]
        {
            let path = PathBuf::from(path);
            return Endpoint::from_static("http://plugin.invalid")
                .connect_timeout(Duration::from_secs(5))
                .connect_with_connector(tower::service_fn(move |_| {
                    let path = path.clone();
                    async move {
                        let stream = tokio::net::UnixStream::connect(path).await?;
                        Ok::<_, std::io::Error>(hyper_util::rt::TokioIo::new(stream))
                    }
                }))
                .await
                .map_err(|e| format!("cannot connect to {address}: {e}"));
        }
        #[cfg(not(unix))]
        {
            let _ = path;
            return Err(format!("{address}: Unix sockets are not available on this platform"));
        }
    }
    Endpoint::from_shared(format!("http://{address}"))
        .map_err(|e| format!("bad address {address:?}: {e}"))?
        .connect_timeout(Duration::from_secs(5))
        .tcp_nodelay(true)
        .connect()
        .await
        .map_err(|e| format!("cannot connect to {address}: {e}"))
}

/// `Hello`: the process speaks this protocol and serves the drivers it was started for.
pub async fn hello(client: &mut Client, drivers: &[String]) -> Result<(), String> {
    let req = proto::HelloRequest { protocol: PROTOCOL.into(), engine: opencap_core::FIRM_VERSION.into() };
    let reply = tokio::time::timeout(HELLO_TIMEOUT, client.hello(req))
        .await
        .map_err(|_| "Hello timed out".to_string())?
        .map_err(|s| format!("Hello failed: {}", s.message()))?
        .into_inner();
    if reply.protocol != PROTOCOL {
        return Err(format!("speaks {:?}, not {PROTOCOL}", reply.protocol));
    }
    if let Some(missing) = drivers.iter().find(|d| !reply.drivers.contains(d)) {
        return Err(format!("does not serve driver {missing:?} (serves {:?})", reply.drivers));
    }
    Ok(())
}

/// Starts the command from the config as a child process.
pub struct ProcessLauncher;

#[async_trait]
impl Launcher for ProcessLauncher {
    async fn launch(&self, spec: &LaunchSpec) -> Result<Launched, String> {
        let program = spec.command.first().ok_or("empty plugin command")?;
        let (dir, mut args) = match spec.transport {
            Transport::Unix => {
                let dir = PrivateDir::create().map_err(|e| format!("cannot create the socket directory: {e}"))?;
                let sock = dir.0.join("plugin.sock").display().to_string();
                (Some(dir), vec!["--socket".to_string(), sock])
            }
            Transport::Tcp => (None, vec!["--listen".to_string(), "127.0.0.1:0".to_string()]),
        };
        for d in &spec.drivers {
            args.extend(["--driver".to_string(), d.clone()]);
        }
        args.push("--exit-on-stdin-eof".into());
        let mut child = Command::new(program)
            .args(&spec.command[1..])
            .args(&args)
            .env(TOKEN_ENV, &spec.token)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| format!("{program}: {e}"))?;
        let pid = child.id();
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().ok_or("no stdout pipe")?;
        let stderr = child.stderr.take().ok_or("no stderr pipe")?;
        let tail: Tail = Arc::default();
        forward(stderr, spec.label.clone(), "stderr", Some(tail.clone()));
        let mut lines = BufReader::new(stdout).lines();
        let wait_ready = async {
            while let Some(line) = lines.next_line().await? {
                if let Some(rest) = line.strip_prefix(READY) {
                    return Ok(Some(rest.trim().to_string()));
                }
                info!(plugin = %spec.label, stream = "stdout", line = %line, "plugin.log");
            }
            Ok::<_, std::io::Error>(None)
        };
        let ready = match tokio::time::timeout(spec.start_timeout, wait_ready).await {
            Err(_) => {
                return Err(format!("not ready within {:.0} s{}", spec.start_timeout.as_secs_f64(), tail_text(&tail)))
            }
            Ok(Err(e)) => return Err(format!("reading its stdout: {e}")),
            Ok(Ok(None)) => {
                let status = child.wait().await.map(|s| s.to_string()).unwrap_or_else(|e| e.to_string());
                tokio::time::sleep(Duration::from_millis(50)).await; // let stderr drain into the tail
                return Err(format!("exited before it was ready ({status}){}", tail_text(&tail)));
            }
            Ok(Ok(Some(ready))) => ready,
        };
        let stdout = lines.into_inner();
        forward(stdout, spec.label.clone(), "stdout", None);
        let info: Json = serde_json::from_str(&ready).map_err(|e| format!("bad ready line {ready:?}: {e}"))?;
        if info["protocol"] != PROTOCOL {
            return Err(format!("ready line announces protocol {}, not {PROTOCOL}", info["protocol"]));
        }
        let address = info["address"].as_str().ok_or("ready line has no address")?.to_string();
        let mut client = client(connect(&address).await?, &spec.token);
        hello(&mut client, &spec.drivers).await?;

        let (exit_tx, exited) = watch::channel(None);
        let (kill, mut kill_rx) = oneshot::channel::<()>();
        let label = spec.label.clone();
        tokio::spawn(async move {
            let status = tokio::select! {
                s = child.wait() => s,
                _ = &mut kill_rx => {
                    let _ = child.start_kill();
                    child.wait().await
                }
            };
            let text = status.map(|s| s.to_string()).unwrap_or_else(|e| e.to_string());
            info!(plugin = %label, pid, status = %text, "plugin.exited");
            let _ = exit_tx.send(Some(text));
        });
        Ok(Launched { client, process: Some(Process { pid, exited, kill: Some(kill), stdin, _dir: dir }) })
    }
}

struct Live {
    client: Client,
    process: Option<Process>,
    generation: u64,
    started: Instant,
}

struct State {
    live: Option<Live>,
    delay: Duration,
    retry_at: Option<Instant>,
    stopped: bool,
}

/// Supervises the plugin process of one channel. See the module docs.
pub struct PluginHost {
    spec: LaunchSpec,
    launcher: Arc<dyn Launcher>,
    state: Mutex<State>,
}

impl PluginHost {
    pub fn new(spec: LaunchSpec, launcher: Arc<dyn Launcher>) -> Arc<Self> {
        Arc::new(Self {
            spec,
            launcher,
            state: Mutex::new(State { live: None, delay: RESTART_MIN, retry_at: None, stopped: false }),
        })
    }

    pub fn label(&self) -> &str {
        &self.spec.label
    }

    pub fn spec(&self) -> &LaunchSpec {
        &self.spec
    }

    /// End the live generation and schedule the next start with backoff.
    fn lose(&self, st: &mut State, why: &str) {
        let Some(live) = st.live.take() else { return };
        if live.started.elapsed() >= STABLE_AFTER {
            st.delay = RESTART_MIN;
        }
        warn!(plugin = %self.spec.label, generation = live.generation, why, restart_in_s = st.delay.as_secs_f64(),
              "plugin.lost");
        st.retry_at = Some(Instant::now() + st.delay);
        st.delay = (st.delay * 2).min(RESTART_MAX);
        drop(live); // kills the process if it still runs
    }

    fn reap(&self, st: &mut State) {
        let exited = st.live.as_ref().and_then(|l| l.process.as_ref()).and_then(Process::exited);
        if let Some(status) = exited {
            self.lose(st, &format!("process exited ({status})"));
        }
    }

    /// The running plugin's client and generation, starting the plugin if none runs.
    pub async fn ensure(&self) -> Result<(Client, u64), PluginError> {
        let mut st = self.state.lock().await;
        if st.stopped {
            return Err(PluginError::Stopped);
        }
        self.reap(&mut st);
        if let Some(live) = &st.live {
            return Ok((live.client.clone(), live.generation));
        }
        if let Some(at) = st.retry_at {
            let now = Instant::now();
            if at > now {
                return Err(PluginError::Backoff((at - now).as_secs_f64()));
            }
        }
        match self.launcher.launch(&self.spec).await {
            Ok(launched) => {
                st.retry_at = None;
                let generation = NEXT_GENERATION.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let pid = launched.process.as_ref().and_then(|p| p.pid);
                info!(plugin = %self.spec.label, generation, pid, drivers = ?self.spec.drivers, "plugin.started");
                let client = launched.client.clone();
                st.live = Some(Live { client, process: launched.process, generation, started: Instant::now() });
                Ok((launched.client, generation))
            }
            Err(e) => {
                warn!(plugin = %self.spec.label, error = %e, restart_in_s = st.delay.as_secs_f64(), "plugin.start_failed");
                st.retry_at = Some(Instant::now() + st.delay);
                st.delay = (st.delay * 2).min(RESTART_MAX);
                Err(PluginError::Start(e))
            }
        }
    }

    /// The running plugin's client and generation, without starting one.
    pub async fn current(&self) -> Option<(Client, u64)> {
        let mut st = self.state.lock().await;
        self.reap(&mut st);
        st.live.as_ref().map(|l| (l.client.clone(), l.generation))
    }

    /// Process id of the running plugin process, if any.
    pub async fn pid(&self) -> Option<u32> {
        self.state.lock().await.live.as_ref().and_then(|l| l.process.as_ref()).and_then(|p| p.pid)
    }

    /// A call on generation `generation` failed in transport: that process is gone or unusable.
    pub async fn failed(&self, generation: u64, why: &str) {
        let mut st = self.state.lock().await;
        if st.live.as_ref().is_some_and(|l| l.generation == generation) {
            self.lose(&mut st, why);
        }
    }

    /// Stop the plugin for good (engine stop, plugin removed from the config).
    pub async fn shutdown(&self) {
        let live = {
            let mut st = self.state.lock().await;
            st.stopped = true;
            st.live.take()
        };
        if let Some(Live { process: Some(p), generation, .. }) = live {
            info!(plugin = %self.spec.label, generation, "plugin.stopping");
            p.stop(STOP_GRACE).await;
        }
    }
}
