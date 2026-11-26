//! The bundled engine (#58, ADR-0010 (a)): single-box install. The installers ship the Rust
//! engine (`opencap-engine`) next to the app as a Tauri `externalBin`; this module starts and
//! stops it.
//!
//! - **Config and outbox** live in the user's local app data (`<local data>/engine/`: `config/`,
//!   `data/`, `logs/`), never in the install directory, so an upgrade or a reinstall keeps them.
//!   They are created on the first start and never overwritten afterwards.
//! - **Port:** the engine listens on 127.0.0.1 only. The first start picks a free port; later
//!   starts keep it while it is free and pick a new one when something else took it. The port
//!   is the one `poller.yaml` line marked [`PORT_MARKER`]; nothing else in that file is touched.
//! - **Token:** a random 256-bit token, passed to the engine in `OPENCAP_API_TOKEN` (its default
//!   `api.token_env`) and kept in the OS keychain for the launcher's "Engine on this computer"
//!   profile, bound to that profile's address. Without a keychain it is kept in memory until the
//!   app closes. It is never written to a file.
//! - **Stop:** SIGTERM and up to [`STOP_TIMEOUT`] for a clean shutdown on Unix, then a kill. On
//!   Windows the process is terminated (it has no console to send Ctrl+C to); the outbox is
//!   SQLite, so queued messages survive. The engine is also stopped when the app exits.
//!
//! Read-only towards PLCs, like the engine itself.

use std::fs::{self, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;

/// The launcher profile of the bundled engine (fixed id, re-created on every start).
pub const PROFILE_ID: &str = "bundled";
pub const PROFILE_NAME: &str = "Engine on this computer";
/// Marks the one `poller.yaml` line whose port the app manages.
pub const PORT_MARKER: &str = "# port managed by opencap desktop";
/// The engine's default token variable (`api.token_env`).
pub const TOKEN_ENV: &str = "OPENCAP_API_TOKEN";
/// Overrides where the engine binary is looked for (development, tests).
pub const BIN_ENV: &str = "OPENCAP_DESKTOP_ENGINE_BIN";
pub const STOP_TIMEOUT: Duration = Duration::from_secs(10);
pub const START_TIMEOUT: Duration = Duration::from_secs(20);

const ENGINE_EXE: &str = if cfg!(windows) {
    "opencap-engine.exe"
} else {
    "opencap-engine"
};

/// Where the bundled engine's files are.
#[derive(Debug, Clone)]
pub struct Layout {
    pub root: PathBuf,
}

impl Layout {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }
    pub fn config_dir(&self) -> PathBuf {
        self.root.join("config")
    }
    pub fn poller_yaml(&self) -> PathBuf {
        self.config_dir().join("poller.yaml")
    }
    pub fn log_file(&self) -> PathBuf {
        self.root.join("logs").join("engine.log")
    }

    /// Create the directories and, only where missing, a starting config for `port`.
    /// Returns true when it wrote a new `poller.yaml`.
    pub fn ensure(&self, port: u16) -> io::Result<bool> {
        for dir in [
            self.config_dir(),
            self.root.join("data"),
            self.root.join("logs"),
        ] {
            fs::create_dir_all(dir)?;
        }
        let devices = self.config_dir().join("devices.json");
        if !devices.exists() {
            fs::write(&devices, "{\"channels\": []}\n")?;
        }
        let poller = self.poller_yaml();
        if poller.exists() {
            return Ok(false);
        }
        fs::write(&poller, poller_template(port))?;
        Ok(true)
    }

    /// The port on the marked line of `poller.yaml`.
    pub fn configured_port(&self) -> Result<u16, String> {
        let text = fs::read_to_string(self.poller_yaml()).map_err(|e| e.to_string())?;
        let line = marked_line(&text).ok_or_else(|| self.marker_missing())?;
        parse_port_line(line).ok_or_else(|| format!("cannot read the port in {line:?}"))
    }

    /// Rewrite only the marked line with a new port.
    pub fn set_port(&self, port: u16) -> Result<(), String> {
        let path = self.poller_yaml();
        let text = fs::read_to_string(&path).map_err(|e| e.to_string())?;
        let old = marked_line(&text).ok_or_else(|| self.marker_missing())?;
        let indent: String = old.chars().take_while(|c| c.is_whitespace()).collect();
        let new_text = text.replacen(old, &format!("{indent}port: {port}  {PORT_MARKER}"), 1);
        fs::write(&path, new_text).map_err(|e| e.to_string())
    }

    fn marker_missing(&self) -> String {
        format!(
            "{} has no line marked {PORT_MARKER:?} under api:, so the app cannot manage its \
             port. Put `port: 8765  {PORT_MARKER}` back under `api:`.",
            self.poller_yaml().display()
        )
    }
}

fn marked_line(text: &str) -> Option<&str> {
    text.lines().find(|l| l.contains(PORT_MARKER))
}

fn parse_port_line(line: &str) -> Option<u16> {
    let value = line.split('#').next()?.trim().strip_prefix("port:")?.trim();
    value.parse().ok().filter(|p| *p != 0)
}

/// The `poller.yaml` the first start writes. Loopback only; secrets only by variable name.
pub fn poller_template(port: u16) -> String {
    format!(
        "# poller.yaml for the engine bundled with opencap desktop (#58).\n\
         # Edit freely, except: keep the marked `port:` line (the app picks a free port there)\n\
         # and keep `bind: 127.0.0.1` (the app passes the API token in {TOKEN_ENV}).\n\
         # Full reference: config/poller.yaml in the opencap repository.\n\
         \n\
         instance: opencap-desktop\n\
         \n\
         mqtt:\n\
         \x20 host: localhost\n\
         \x20 port: 1883\n\
         \n\
         saf:\n\
         \x20 path: data/saf.sqlite\n\
         \n\
         api:\n\
         \x20 bind: 127.0.0.1\n\
         \x20 port: {port}  {PORT_MARKER}\n\
         \x20 token_env: {TOKEN_ENV}\n"
    )
}

/// A port nothing listens on right now, on 127.0.0.1.
pub fn free_loopback_port() -> io::Result<u16> {
    Ok(TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port())
}

pub fn port_is_free(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// 256 random bits from the OS, hex-encoded.
pub fn new_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| format!("no random source: {e}"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// The engine binary: `$OPENCAP_DESKTOP_ENGINE_BIN`, else next to the app's own executable
/// (where the installers put Tauri's external binaries).
pub fn engine_binary() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os(BIN_ENV) {
        return Some(PathBuf::from(p));
    }
    Some(std::env::current_exe().ok()?.parent()?.join(ENGINE_EXE))
}

pub fn base_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

/// What the launcher shows for the bundled engine.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct LocalStatus {
    pub available: bool,
    pub running: bool,
    pub pid: Option<u32>,
    pub base_url: Option<String>,
    pub binary: String,
    pub config_dir: String,
    pub log_file: String,
    /// Why the last run ended, when it ended by itself.
    pub last_exit: Option<String>,
    /// Something to tell the user (keychain unavailable, port changed, ...).
    pub notice: Option<String>,
}

struct Running {
    child: Child,
    port: u16,
}

/// Starts and stops the bundled engine. One instance per app.
pub struct LocalEngine {
    layout: Layout,
    binary: Option<PathBuf>,
    run: Mutex<Option<Running>>,
    token: Mutex<Option<String>>,
    last_exit: Mutex<Option<String>>,
}

impl LocalEngine {
    pub fn new(layout: Layout, binary: Option<PathBuf>) -> Self {
        Self {
            layout,
            binary,
            run: Mutex::new(None),
            token: Mutex::new(None),
            last_exit: Mutex::new(None),
        }
    }

    pub fn layout(&self) -> &Layout {
        &self.layout
    }

    /// The token the running engine accepts (also when the keychain could not keep it).
    pub fn token(&self) -> Option<String> {
        self.token.lock().unwrap().clone()
    }

    fn available(&self) -> bool {
        self.binary.as_deref().is_some_and(Path::is_file)
    }

    pub fn status(&self) -> LocalStatus {
        let mut run = self.run.lock().unwrap();
        self.reap(&mut run);
        self.status_of(&run, None)
    }

    fn status_of(&self, run: &Option<Running>, notice: Option<String>) -> LocalStatus {
        LocalStatus {
            available: self.available(),
            running: run.is_some(),
            pid: run.as_ref().map(|r| r.child.id()),
            base_url: run.as_ref().map(|r| base_url(r.port)),
            binary: self
                .binary
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_default(),
            config_dir: self.layout.config_dir().display().to_string(),
            log_file: self.layout.log_file().display().to_string(),
            last_exit: self.last_exit.lock().unwrap().clone(),
            notice,
        }
    }

    /// Forget a child that has exited by itself, keeping why.
    fn reap(&self, run: &mut Option<Running>) {
        if let Some(r) = run.as_mut() {
            if let Ok(Some(status)) = r.child.try_wait() {
                *self.last_exit.lock().unwrap() = Some(format!(
                    "the engine exited ({status}); see {}",
                    self.layout.log_file().display()
                ));
                *run = None;
            }
        }
    }

    /// Start the engine (no-op when it runs). `remember` stores the profile and token; it
    /// returns a notice for the user when the keychain could not keep the token.
    pub fn start(
        &self,
        remember: impl FnOnce(&str, &str) -> Option<String>,
    ) -> Result<LocalStatus, String> {
        let mut run = self.run.lock().unwrap();
        self.reap(&mut run);
        if run.is_some() {
            return Ok(self.status_of(&run, None));
        }
        let binary = self
            .binary
            .clone()
            .filter(|p| p.is_file())
            .ok_or_else(|| "this installation has no bundled engine".to_string())?;

        let first_port = free_loopback_port().map_err(|e| e.to_string())?;
        let created = self
            .layout
            .ensure(first_port)
            .map_err(|e| format!("cannot create {}: {e}", self.layout.root.display()))?;
        let mut port = self.layout.configured_port()?;
        let mut notices = Vec::new();
        if !created && !port_is_free(port) {
            let new_port = free_loopback_port().map_err(|e| e.to_string())?;
            self.layout.set_port(new_port)?;
            notices.push(format!(
                "Port {port} was taken by another program; the engine now uses {new_port}."
            ));
            port = new_port;
        }

        let token = new_token()?;
        let url = base_url(port);
        if let Some(n) = remember(&url, &token) {
            notices.push(n);
        }

        let log_path = self.layout.log_file();
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| format!("cannot open {}: {e}", log_path.display()))?;
        let log_start = log.metadata().map(|m| m.len()).unwrap_or(0);
        let mut cmd = Command::new(&binary);
        cmd.arg("run")
            .arg("--config")
            .arg(self.layout.config_dir())
            .current_dir(&self.layout.root) // relative saf.path resolves from here
            .env(TOKEN_ENV, &token)
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("cannot start {}: {e}", binary.display()))?;

        let deadline = Instant::now() + START_TIMEOUT;
        loop {
            if let Ok(Some(status)) = child.try_wait() {
                let tail = log_tail(&log_path, log_start, 2000);
                return Err(format!("the engine stopped right away ({status}):\n{tail}"));
            }
            if crate::engine::check(&url, Some(&token)).is_ok() {
                break;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                let tail = log_tail(&log_path, log_start, 2000);
                return Err(format!(
                    "the engine did not answer on {url} within {} s:\n{tail}",
                    START_TIMEOUT.as_secs()
                ));
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        *self.token.lock().unwrap() = Some(token);
        *self.last_exit.lock().unwrap() = None;
        *run = Some(Running { child, port });
        let notice = (!notices.is_empty()).then(|| notices.join(" "));
        Ok(self.status_of(&run, notice))
    }

    /// Stop the engine (no-op when it is not running).
    pub fn stop(&self) -> Result<LocalStatus, String> {
        let mut run = self.run.lock().unwrap();
        if let Some(mut r) = run.take() {
            stop_child(&mut r.child, STOP_TIMEOUT)?;
        }
        *self.token.lock().unwrap() = None;
        Ok(self.status_of(&run, None))
    }
}

impl Drop for LocalEngine {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[cfg(unix)]
fn stop_child(child: &mut Child, timeout: Duration) -> Result<(), String> {
    if child.try_wait().map_err(|e| e.to_string())?.is_some() {
        return Ok(());
    }
    let pid = i32::try_from(child.id()).map_err(|e| e.to_string())?;
    // SAFETY: kill(2) on our own child's pid; it has not been reaped yet (try_wait above).
    unsafe { libc::kill(pid, libc::SIGTERM) };
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    child.kill().map_err(|e| e.to_string())?;
    child.wait().map(|_| ()).map_err(|e| e.to_string())
}

#[cfg(not(unix))]
fn stop_child(child: &mut Child, _timeout: Duration) -> Result<(), String> {
    if child.try_wait().map_err(|e| e.to_string())?.is_some() {
        return Ok(());
    }
    child.kill().map_err(|e| e.to_string())?;
    child.wait().map(|_| ()).map_err(|e| e.to_string())
}

/// What the engine wrote to its log since `from`, at most the last `max` bytes.
fn log_tail(path: &Path, from: u64, max: u64) -> String {
    let mut text = String::new();
    if let Ok(mut f) = fs::File::open(path) {
        let len = f.metadata().map(|m| m.len()).unwrap_or(0);
        let start = from.max(len.saturating_sub(max));
        if f.seek(SeekFrom::Start(start)).is_ok() {
            let _ = f.read_to_string(&mut text);
        }
    }
    if text.trim().is_empty() {
        format!("(nothing in {})", path.display())
    } else {
        text
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn layout() -> (tempfile::TempDir, Layout) {
        let dir = tempfile::tempdir().unwrap();
        let l = Layout::new(dir.path().join("engine"));
        (dir, l)
    }

    #[test]
    fn the_first_start_writes_a_loopback_config_and_later_ones_keep_it() {
        let (_d, l) = layout();
        assert!(l.ensure(18765).unwrap());
        assert!(l.config_dir().join("devices.json").is_file());
        assert!(l.root.join("data").is_dir() && l.root.join("logs").is_dir());
        let text = fs::read_to_string(l.poller_yaml()).unwrap();
        assert!(text.contains("bind: 127.0.0.1"));
        assert!(text.contains(&format!("token_env: {TOKEN_ENV}")));
        assert_eq!(l.configured_port().unwrap(), 18765);

        // a user edit (and the outbox) survive the next start, which is what an upgrade sees
        fs::write(
            l.poller_yaml(),
            text.replace("opencap-desktop", "plant-a-desk"),
        )
        .unwrap();
        fs::write(l.root.join("data").join("saf.sqlite"), b"queued").unwrap();
        assert!(!l.ensure(19999).unwrap());
        let kept = fs::read_to_string(l.poller_yaml()).unwrap();
        assert!(kept.contains("instance: plant-a-desk"));
        assert_eq!(l.configured_port().unwrap(), 18765);
        assert_eq!(
            fs::read(l.root.join("data").join("saf.sqlite")).unwrap(),
            b"queued"
        );
    }

    #[test]
    fn set_port_rewrites_only_the_marked_line() {
        let (_d, l) = layout();
        l.ensure(18765).unwrap();
        let before = fs::read_to_string(l.poller_yaml()).unwrap();
        l.set_port(20001).unwrap();
        let after = fs::read_to_string(l.poller_yaml()).unwrap();
        assert_eq!(l.configured_port().unwrap(), 20001);
        assert!(
            after.contains("  port: 1883\n"),
            "the MQTT port is untouched:\n{after}"
        );
        let changed: Vec<_> = before
            .lines()
            .zip(after.lines())
            .filter(|(a, b)| a != b)
            .collect();
        assert_eq!(changed.len(), 1, "{changed:?}");
        assert_eq!(changed[0].1, format!("  port: 20001  {PORT_MARKER}"));
    }

    #[test]
    fn a_config_without_the_marker_is_refused_with_a_fix() {
        let (_d, l) = layout();
        l.ensure(18765).unwrap();
        let text = fs::read_to_string(l.poller_yaml()).unwrap();
        fs::write(l.poller_yaml(), text.replace(PORT_MARKER, "")).unwrap();
        let e = l.configured_port().unwrap_err();
        assert!(e.contains(PORT_MARKER) && e.contains("api:"), "{e}");
        assert!(l.set_port(1).is_err());
    }

    #[test]
    fn port_line_parsing() {
        assert_eq!(
            parse_port_line(&format!("  port: 8765  {PORT_MARKER}")),
            Some(8765)
        );
        assert_eq!(parse_port_line("  port: 0  # x"), None);
        assert_eq!(parse_port_line("  port: http  # x"), None);
        assert_eq!(parse_port_line("  bind: 1  # x"), None);
    }

    #[test]
    fn free_ports_and_tokens() {
        let p = free_loopback_port().unwrap();
        assert!(p > 0 && port_is_free(p));
        let _hold = TcpListener::bind(("127.0.0.1", p)).unwrap();
        assert!(!port_is_free(p));
        let (a, b) = (new_token().unwrap(), new_token().unwrap());
        assert_eq!(a.len(), 64);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn without_a_bundled_binary_start_says_so_and_status_is_unavailable() {
        let (d, l) = layout();
        let eng = LocalEngine::new(l, Some(d.path().join("missing-engine")));
        let st = eng.status();
        assert!(!st.available && !st.running);
        let e = eng.start(|_, _| None).unwrap_err();
        assert!(e.contains("no bundled engine"), "{e}");
        assert!(eng.stop().is_ok());
    }

    /// Starts the real engine when CI provides one (`OPENCAP_TEST_ENGINE_BIN`, desktop.yml):
    /// first start, /health with the token, refusal without it, stop, restart on the same port,
    /// and a taken port moving to a free one.
    #[test]
    fn the_real_engine_starts_answers_with_its_token_and_stops() {
        let Some(bin) = std::env::var_os("OPENCAP_TEST_ENGINE_BIN") else {
            eprintln!("OPENCAP_TEST_ENGINE_BIN not set: real-engine start/stop not exercised here");
            return;
        };
        let (_d, l) = layout();
        let eng = LocalEngine::new(l.clone(), Some(PathBuf::from(bin)));
        let mut remembered = None;
        let st = eng
            .start(|url, token| {
                remembered = Some((url.to_string(), token.to_string()));
                None
            })
            .unwrap();
        assert!(st.running && st.available, "{st:?}");
        let (url, token) = remembered.unwrap();
        assert_eq!(st.base_url.as_deref(), Some(url.as_str()));
        assert_eq!(eng.token().as_deref(), Some(token.as_str()));
        let status = crate::engine::check(&url, Some(&token)).unwrap();
        assert!(
            matches!(status.auth, crate::engine::Auth::Ok { .. }),
            "{status:?}"
        );
        let refused = crate::engine::check(&url, Some("wrong")).unwrap();
        assert_eq!(refused.auth, crate::engine::Auth::BadToken, "{refused:?}");

        let port = l.configured_port().unwrap();
        let st = eng.stop().unwrap();
        assert!(!st.running && eng.token().is_none());
        assert!(port_is_free(port), "the engine released its port");

        let st = eng.start(|_, _| None).unwrap();
        assert_eq!(
            st.base_url,
            Some(base_url(port)),
            "same port on the next start"
        );
        eng.stop().unwrap();

        let _taken = TcpListener::bind(("127.0.0.1", port)).unwrap();
        let st = eng.start(|_, _| None).unwrap();
        assert_ne!(st.base_url, Some(base_url(port)));
        assert!(st
            .notice
            .unwrap_or_default()
            .contains(&format!("Port {port} was taken")));
        assert_ne!(l.configured_port().unwrap(), port);
        eng.stop().unwrap();
    }
}
