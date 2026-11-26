//! The engine as a native Windows service (#57), without a wrapper such as WinSW:
//!
//! ```text
//! opencap-engine service install [--config DIR]   # register: automatic start, restart on failure
//! opencap-engine service uninstall                # stop if running, then remove
//! opencap-engine service run [--config DIR]       # what the service control manager starts
//! ```
//!
//! - **Layout:** `%ProgramData%\opencap\` is the working directory, so relative paths in
//!   `poller.yaml` (the outbox `data/saf.sqlite`) land there; the config defaults to its
//!   `config\` and the log goes to `logs\opencap-engine.log` (JSON lines, a service has no console).
//! - **Stop and shutdown** become the same clean stop as SIGTERM on Linux: the API stops, the
//!   poller stops, the outbox is kept and the `offline` status is published.
//! - **Recovery:** restart after 5 s, 5 s, then 30 s; the failure count resets after a day, and a
//!   non-zero exit counts as a failure too.
//! - **Event log:** source `opencap-engine` under Application, with the message file .NET ships
//!   (one `%1` message for every id) when present, so Event Viewer shows the text as written.
//!   The service writes "started", "stopped" and the error that ended a failed run.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use anyhow::{Context as _, Result};
use clap::Subcommand;
use windows_service::service::{
    ServiceAccess, ServiceAction, ServiceActionType, ServiceControl, ServiceControlAccept, ServiceErrorControl,
    ServiceExitCode, ServiceFailureActions, ServiceFailureResetPeriod, ServiceInfo, ServiceStartType, ServiceState,
    ServiceStatus, ServiceType,
};
use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
use windows_service::{define_windows_service, service_dispatcher};

pub const SERVICE_NAME: &str = "opencap-engine";
const DISPLAY_NAME: &str = "opencap engine";
const DESCRIPTION: &str =
    "opencap read-only PLC connectivity server (Rust engine): polls PLCs, publishes to MQTT. docs/deployment.md";

#[derive(Subcommand)]
pub enum ServiceCmd {
    /// Register the service (automatic start, restart on failure, event log source).
    Install {
        /// Config directory the service reads [default: %ProgramData%\opencap\config]
        #[arg(short, long)]
        config: Option<PathBuf>,
    },
    /// Stop the service if it runs, then remove it and its event log source.
    Uninstall,
    /// Run as the service (the service control manager starts this; not for a console).
    Run {
        #[arg(short, long)]
        config: Option<PathBuf>,
    },
}

/// `%ProgramData%\opencap`.
pub fn program_data() -> PathBuf {
    std::env::var_os("ProgramData")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"))
        .join("opencap")
}

fn config_or_default(config: Option<PathBuf>) -> Result<PathBuf> {
    let dir = config.unwrap_or_else(|| program_data().join("config"));
    // The service starts in another directory: store an absolute path.
    std::path::absolute(&dir).with_context(|| format!("config directory {}", dir.display()))
}

pub fn dispatch(action: ServiceCmd) -> Result<i32> {
    match action {
        ServiceCmd::Install { config } => install(&config_or_default(config)?).map(|()| 0),
        ServiceCmd::Uninstall => uninstall().map(|()| 0),
        ServiceCmd::Run { config } => {
            let _ = CONFIG.set(config_or_default(config)?);
            service_dispatcher::start(SERVICE_NAME, ffi_service_main)
                .context("not started by the service control manager (use `service install`, then `sc start`)")?;
            Ok(*EXIT_CODE.get().unwrap_or(&0))
        }
    }
}

fn install(config: &Path) -> Result<()> {
    if !config.join("poller.yaml").is_file() {
        eprintln!("note: {} has no poller.yaml yet; the service will fail to start until it does", config.display());
    }
    let manager = ServiceManager::local_computer(
        None::<&str>,
        ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE,
    )
    .context("open the service control manager (run as Administrator)")?;
    let info = ServiceInfo {
        name: OsString::from(SERVICE_NAME),
        display_name: OsString::from(DISPLAY_NAME),
        service_type: ServiceType::OWN_PROCESS,
        start_type: ServiceStartType::AutoStart,
        error_control: ServiceErrorControl::Normal,
        executable_path: std::env::current_exe()?,
        launch_arguments: vec!["service".into(), "run".into(), "--config".into(), config.as_os_str().to_owned()],
        dependencies: vec![],
        account_name: None, // LocalSystem
        account_password: None,
    };
    let service = manager
        .create_service(&info, ServiceAccess::CHANGE_CONFIG | ServiceAccess::QUERY_STATUS | ServiceAccess::START)
        .context("create the service")?;
    service.set_description(DESCRIPTION)?;
    let restart = |secs| ServiceAction { action_type: ServiceActionType::Restart, delay: Duration::from_secs(secs) };
    service.update_failure_actions(ServiceFailureActions {
        reset_period: ServiceFailureResetPeriod::After(Duration::from_secs(24 * 3600)),
        reboot_msg: None,
        command: None,
        actions: Some(vec![restart(5), restart(5), restart(30)]),
    })?;
    service.set_failure_actions_on_non_crash_failures(true)?;
    eventlog::register().context("register the event log source")?;
    for dir in ["config", "data", "logs"] {
        std::fs::create_dir_all(program_data().join(dir))?;
    }
    println!("installed {SERVICE_NAME}: config {}, working directory {}", config.display(), program_data().display());
    Ok(())
}

fn uninstall() -> Result<()> {
    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
        .context("open the service control manager (run as Administrator)")?;
    let service = manager
        .open_service(SERVICE_NAME, ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::DELETE)
        .context("open the service (is it installed?)")?;
    if service.query_status()?.current_state != ServiceState::Stopped {
        let _ = service.stop();
        let deadline = Instant::now() + Duration::from_secs(60);
        while service.query_status()?.current_state != ServiceState::Stopped && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(250));
        }
    }
    service.delete()?;
    drop(service);
    let _ = eventlog::unregister();
    println!("removed {SERVICE_NAME}; config, outbox and logs stay in {}", program_data().display());
    Ok(())
}

static CONFIG: OnceLock<PathBuf> = OnceLock::new();
static EXIT_CODE: OnceLock<i32> = OnceLock::new();

define_windows_service!(ffi_service_main, service_main);

fn service_main(_args: Vec<OsString>) {
    let code = match run_service() {
        Ok(code) => code,
        Err(e) => {
            eventlog::report(eventlog::Level::Error, &format!("{SERVICE_NAME} failed: {e:#}"));
            1
        }
    };
    let _ = EXIT_CODE.set(code);
}

fn run_service() -> Result<i32> {
    let (stop_tx, mut stop_rx) = tokio::sync::watch::channel(false);
    let handler = move |control| match control {
        ServiceControl::Stop | ServiceControl::Shutdown | ServiceControl::Preshutdown => {
            let _ = stop_tx.send(true);
            ServiceControlHandlerResult::NoError
        }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    };
    let status = service_control_handler::register(SERVICE_NAME, handler)?;
    let set = move |state, accept, code: u32, wait: Duration| {
        let _ = status.set_service_status(ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: state,
            controls_accepted: accept,
            exit_code: ServiceExitCode::Win32(code),
            checkpoint: 0,
            wait_hint: wait,
            process_id: None,
        });
    };
    set(ServiceState::StartPending, ServiceControlAccept::empty(), 0, Duration::from_secs(30));

    let base = program_data();
    for dir in ["data", "logs"] {
        std::fs::create_dir_all(base.join(dir))?;
    }
    std::env::set_current_dir(&base)?;
    let _ = crate::LOG_FILE.set(base.join("logs").join("opencap-engine.log"));
    let config = CONFIG.get().cloned().unwrap_or_else(|| base.join("config"));

    let stop = async move {
        let _ = stop_rx.wait_for(|stop| *stop).await;
        set(ServiceState::StopPending, ServiceControlAccept::empty(), 0, Duration::from_secs(30));
        "service stop"
    };
    let ready = move || {
        set(ServiceState::Running, ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN, 0, Duration::ZERO);
        eventlog::report(eventlog::Level::Info, &format!("{SERVICE_NAME} started (config {})", config_label()));
    };
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build()?;
    let result = rt.block_on(crate::cmd_run(&config, Vec::new(), stop, ready));
    rt.shutdown_timeout(Duration::from_secs(2));
    let code = match result {
        Ok(code) => code,
        Err(e) => {
            eventlog::report(eventlog::Level::Error, &format!("{SERVICE_NAME} stopped on an error: {e:#}"));
            1
        }
    };
    if code == 0 {
        eventlog::report(eventlog::Level::Info, &format!("{SERVICE_NAME} stopped"));
    }
    set(ServiceState::Stopped, ServiceControlAccept::empty(), code as u32, Duration::ZERO);
    Ok(code)
}

fn config_label() -> String {
    CONFIG.get().map(|p| p.display().to_string()).unwrap_or_default()
}

/// The Application event log source `opencap-engine`.
mod eventlog {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;

    use windows_sys::Win32::Foundation::ERROR_SUCCESS;
    use windows_sys::Win32::System::EventLog::{
        DeregisterEventSource, RegisterEventSourceW, ReportEventW, EVENTLOG_ERROR_TYPE, EVENTLOG_INFORMATION_TYPE,
    };
    use windows_sys::Win32::System::Registry::{
        RegCloseKey, RegCreateKeyExW, RegDeleteKeyW, RegSetValueExW, HKEY, HKEY_LOCAL_MACHINE, KEY_WRITE, REG_DWORD,
        REG_EXPAND_SZ, REG_OPTION_NON_VOLATILE,
    };

    use super::SERVICE_NAME;

    const KEY: &str = r"SYSTEM\CurrentControlSet\Services\EventLog\Application\opencap-engine";
    /// .NET's message file: every event id has the message "%1".
    const DOTNET_MESSAGES: &str = r"%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\EventLogMessages.dll";

    pub enum Level {
        Info,
        Error,
    }

    fn wide(s: &str) -> Vec<u16> {
        OsStr::new(s).encode_wide().chain(Some(0)).collect()
    }

    pub fn register() -> std::io::Result<()> {
        let message_file = if std::path::Path::new(
            &DOTNET_MESSAGES
                .replace("%SystemRoot%", &std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".into())),
        )
        .is_file()
        {
            DOTNET_MESSAGES.to_string()
        } else {
            std::env::current_exe()?.display().to_string()
        };
        let mut key: HKEY = std::ptr::null_mut();
        let key_name = wide(KEY);
        // SAFETY: valid NUL-terminated wide strings and an out-pointer for the key handle.
        let rc = unsafe {
            RegCreateKeyExW(
                HKEY_LOCAL_MACHINE,
                key_name.as_ptr(),
                0,
                std::ptr::null(),
                REG_OPTION_NON_VOLATILE,
                KEY_WRITE,
                std::ptr::null(),
                &mut key,
                std::ptr::null_mut(),
            )
        };
        if rc != ERROR_SUCCESS {
            return Err(std::io::Error::from_raw_os_error(rc as i32));
        }
        let file = wide(&message_file);
        let types: u32 = 7; // error, warning, information
        let name_file = wide("EventMessageFile");
        let name_types = wide("TypesSupported");
        // SAFETY: `key` is an open key; the data pointers and byte lengths match their buffers.
        let (rc1, rc2) = unsafe {
            (
                RegSetValueExW(
                    key,
                    name_file.as_ptr(),
                    0,
                    REG_EXPAND_SZ,
                    file.as_ptr().cast(),
                    (file.len() * 2) as u32,
                ),
                RegSetValueExW(key, name_types.as_ptr(), 0, REG_DWORD, (&types as *const u32).cast(), 4),
            )
        };
        // SAFETY: closing the key opened above.
        unsafe { RegCloseKey(key) };
        for rc in [rc1, rc2] {
            if rc != ERROR_SUCCESS {
                return Err(std::io::Error::from_raw_os_error(rc as i32));
            }
        }
        Ok(())
    }

    pub fn unregister() -> std::io::Result<()> {
        let key_name = wide(KEY);
        // SAFETY: a valid NUL-terminated wide string.
        let rc = unsafe { RegDeleteKeyW(HKEY_LOCAL_MACHINE, key_name.as_ptr()) };
        if rc != ERROR_SUCCESS {
            return Err(std::io::Error::from_raw_os_error(rc as i32));
        }
        Ok(())
    }

    /// Write one event; a failure (source not registered, no rights) is ignored.
    pub fn report(level: Level, text: &str) {
        let source = wide(SERVICE_NAME);
        let message = wide(text);
        let strings = [message.as_ptr()];
        let kind = match level {
            Level::Info => EVENTLOG_INFORMATION_TYPE,
            Level::Error => EVENTLOG_ERROR_TYPE,
        };
        // SAFETY: valid wide strings; the handle is checked and deregistered.
        unsafe {
            let handle = RegisterEventSourceW(std::ptr::null(), source.as_ptr());
            if handle.is_null() {
                return;
            }
            ReportEventW(handle, kind, 0, 1, std::ptr::null_mut(), 1, 0, strings.as_ptr(), std::ptr::null());
            DeregisterEventSource(handle);
        }
    }
}
