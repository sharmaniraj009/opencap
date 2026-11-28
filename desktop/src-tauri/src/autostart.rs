//! Start the app when the user logs in (`opencap-desktop --minimized`), without a plugin and
//! without a Tauri permission: it is a file in the user's autostart folder on Linux and a value
//! under `HKCU\...\Run` on Windows, both per user, both removed again by switching it off.
//! The launcher window never touches either itself: it calls the app's `set_autostart` command.

use std::path::{Path, PathBuf};

pub const ENTRY_NAME: &str = "opencap-desktop";
pub const MINIMIZED: &str = "--minimized";

/// The program to start at login: the AppImage itself when running from one (the path of the
/// mounted copy changes on every run), otherwise this executable.
pub fn launch_target() -> Result<PathBuf, String> {
    if let Some(appimage) = std::env::var_os("APPIMAGE").filter(|v| !v.is_empty()) {
        return Ok(PathBuf::from(appimage));
    }
    std::env::current_exe().map_err(|e| format!("cannot find this program's path: {e}"))
}

/// A value of an `Exec=` line (freedesktop Desktop Entry spec): quoted, with `"`, `` ` ``, `$`
/// and `\` escaped and `%` doubled so no field code is read out of a path.
pub fn quote_exec(arg: &str) -> String {
    let mut out = String::from("\"");
    for c in arg.chars() {
        match c {
            '"' | '`' | '$' | '\\' => {
                out.push('\\');
                out.push(c);
            }
            '%' => out.push_str("%%"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

/// The `.desktop` file for the autostart folder.
pub fn desktop_entry(exe: &Path) -> String {
    format!(
        "[Desktop Entry]\nType=Application\nName=opencap\nComment=opencap desktop: opencap engines and their web UI\n\
         Exec={} {MINIMIZED}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
        quote_exec(&exe.display().to_string())
    )
}

/// The data of the Windows `Run` value: the path in quotes (it may contain spaces), then the flag.
pub fn run_value(exe: &Path) -> String {
    format!("\"{}\" {MINIMIZED}", exe.display())
}

/// Arguments of `reg.exe` that set the per-user Run value.
pub fn reg_add_args(exe: &Path) -> Vec<String> {
    vec![
        "add".into(),
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run".into(),
        "/v".into(),
        ENTRY_NAME.into(),
        "/t".into(),
        "REG_SZ".into(),
        "/d".into(),
        run_value(exe),
        "/f".into(),
    ]
}

pub fn reg_delete_args() -> Vec<String> {
    vec![
        "delete".into(),
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run".into(),
        "/v".into(),
        ENTRY_NAME.into(),
        "/f".into(),
    ]
}

pub fn reg_query_args() -> Vec<String> {
    vec![
        "query".into(),
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run".into(),
        "/v".into(),
        ENTRY_NAME.into(),
    ]
}

#[cfg(target_os = "linux")]
fn entry_path(config_dir: &Path) -> PathBuf {
    config_dir
        .join("autostart")
        .join(format!("{ENTRY_NAME}.desktop"))
}

/// Is the app set to start at login?
pub fn is_enabled(config_dir: &Path) -> Result<bool, String> {
    #[cfg(target_os = "linux")]
    {
        Ok(entry_path(config_dir).is_file())
    }
    #[cfg(windows)]
    {
        let _ = config_dir;
        Ok(reg(&reg_query_args())?.0)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = config_dir;
        Err(unsupported())
    }
}

/// Turn start at login on or off; returns the new state.
pub fn set_enabled(config_dir: &Path, exe: &Path, enabled: bool) -> Result<bool, String> {
    #[cfg(target_os = "linux")]
    {
        let path = entry_path(config_dir);
        if enabled {
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir)
                    .map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
            }
            std::fs::write(&path, desktop_entry(exe))
                .map_err(|e| format!("cannot write {}: {e}", path.display()))?;
        } else if path.exists() {
            std::fs::remove_file(&path)
                .map_err(|e| format!("cannot remove {}: {e}", path.display()))?;
        }
        Ok(enabled)
    }
    #[cfg(windows)]
    {
        let _ = config_dir;
        if enabled {
            let (ok, out) = reg(&reg_add_args(exe))?;
            if !ok {
                return Err(format!("could not set the Run value: {out}"));
            }
        } else if reg(&reg_query_args())?.0 {
            let (ok, out) = reg(&reg_delete_args())?;
            if !ok {
                return Err(format!("could not remove the Run value: {out}"));
            }
        }
        Ok(enabled)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        let _ = (config_dir, exe, enabled);
        Err(unsupported())
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn unsupported() -> String {
    "start at login is not available on this system".into()
}

/// `reg.exe` without a console window: (succeeded, its output).
#[cfg(windows)]
fn reg(args: &[String]) -> Result<(bool, String), String> {
    use std::os::windows::process::CommandExt;
    let out = std::process::Command::new("reg")
        .args(args)
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output()
        .map_err(|e| format!("cannot run reg.exe: {e}"))?;
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    Ok((out.status.success(), text.trim().to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exec_paths_are_quoted_against_spaces_and_field_codes() {
        assert_eq!(quote_exec("/opt/open cap/app"), "\"/opt/open cap/app\"");
        assert_eq!(quote_exec("/a\"b$c`d\\e%f"), "\"/a\\\"b\\$c\\`d\\\\e%%f\"");
    }

    #[test]
    fn the_desktop_entry_starts_the_app_minimized() {
        let text = desktop_entry(Path::new("/usr/bin/opencap-desktop"));
        assert!(text.starts_with("[Desktop Entry]\nType=Application\n"));
        assert!(
            text.contains("Exec=\"/usr/bin/opencap-desktop\" --minimized\n"),
            "{text}"
        );
        assert!(text.contains("Terminal=false"));
    }

    #[test]
    fn the_windows_run_value_quotes_the_path() {
        let exe = Path::new(r"C:\Program Files\opencap-desktop\opencap-desktop.exe");
        assert_eq!(
            run_value(exe),
            r#""C:\Program Files\opencap-desktop\opencap-desktop.exe" --minimized"#
        );
        let args = reg_add_args(exe);
        assert_eq!(
            &args[..4],
            [
                "add",
                r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                "/v",
                "opencap-desktop"
            ]
        );
        assert!(args.contains(&"/f".to_string()) && args.contains(&run_value(exe)));
        assert_eq!(reg_delete_args()[0], "delete");
        assert_eq!(reg_query_args()[0], "query");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_autostart_is_a_file_that_comes_and_goes() {
        let dir = tempfile::tempdir().unwrap();
        let exe = Path::new("/usr/bin/opencap-desktop");
        assert!(!is_enabled(dir.path()).unwrap());
        assert!(set_enabled(dir.path(), exe, true).unwrap());
        assert!(is_enabled(dir.path()).unwrap());
        let text =
            std::fs::read_to_string(dir.path().join("autostart/opencap-desktop.desktop")).unwrap();
        assert!(text.contains("--minimized"));
        assert!(!set_enabled(dir.path(), exe, false).unwrap());
        assert!(!is_enabled(dir.path()).unwrap());
        assert!(
            !set_enabled(dir.path(), exe, false).unwrap(),
            "switching off twice is fine"
        );
    }
}
