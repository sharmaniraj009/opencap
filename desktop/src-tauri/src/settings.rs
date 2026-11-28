//! The app's own preferences (`settings.json` in the app config directory, next to
//! `profiles.json`): what the launcher does when it starts and when its window is closed. No
//! secrets live here (tokens stay in the keychain), only switches and the id of the engine
//! profile that was opened last.

use std::path::PathBuf;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// Closing the launcher window hides it to the tray instead of quitting (only honoured when the
    /// tray icon could be created: otherwise the user would have no way back to the window).
    pub keep_in_tray: bool,
    /// Start the engine bundled with the app as soon as the app starts.
    pub start_engine_on_launch: bool,
    /// Open the web UI of the engine opened last, as soon as the app starts.
    pub open_last_on_launch: bool,
    /// The profile id opened last (a profile that was deleted since is simply not found).
    pub last_profile: Option<String>,
}

/// What the launcher may change. `last_profile` is only ever set by opening an engine.
#[derive(Debug, Default, Deserialize)]
#[serde(default)]
pub struct SettingsPatch {
    pub keep_in_tray: Option<bool>,
    pub start_engine_on_launch: Option<bool>,
    pub open_last_on_launch: Option<bool>,
}

pub struct SettingsStore {
    path: PathBuf,
    inner: Mutex<Settings>,
}

impl SettingsStore {
    /// Reads the file; a missing, unreadable or malformed file gives the defaults (the app must
    /// start whatever is in it).
    pub fn load(path: PathBuf) -> Self {
        let settings = std::fs::read_to_string(&path)
            .ok()
            .and_then(|text| serde_json::from_str::<Settings>(&text).ok())
            .unwrap_or_default();
        SettingsStore {
            path,
            inner: Mutex::new(settings),
        }
    }

    pub fn get(&self) -> Settings {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    pub fn apply(&self, patch: SettingsPatch) -> Result<Settings, String> {
        self.change(|s| {
            if let Some(v) = patch.keep_in_tray {
                s.keep_in_tray = v;
            }
            if let Some(v) = patch.start_engine_on_launch {
                s.start_engine_on_launch = v;
            }
            if let Some(v) = patch.open_last_on_launch {
                s.open_last_on_launch = v;
            }
        })
    }

    pub fn set_last_profile(&self, id: &str) -> Result<Settings, String> {
        self.change(|s| s.last_profile = Some(id.to_string()))
    }

    fn change(&self, f: impl FnOnce(&mut Settings)) -> Result<Settings, String> {
        let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let mut next = guard.clone();
        f(&mut next);
        if next != *guard {
            write_atomically(&self.path, &next)?;
            *guard = next;
        }
        Ok(guard.clone())
    }
}

fn write_atomically(path: &PathBuf, settings: &Settings) -> Result<(), String> {
    let text = serde_json::to_string_pretty(settings).map_err(|e| e.to_string())?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)
            .map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, text).map_err(|e| format!("cannot write {}: {e}", tmp.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    std::fs::rename(&tmp, path).map_err(|e| format!("cannot replace {}: {e}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_install_changes_nothing_about_how_the_app_behaves() {
        let dir = tempfile::tempdir().unwrap();
        let store = SettingsStore::load(dir.path().join("settings.json"));
        assert_eq!(store.get(), Settings::default());
        assert!(
            !store.get().keep_in_tray
                && !store.get().start_engine_on_launch
                && !store.get().open_last_on_launch
        );
    }

    #[test]
    fn changes_are_kept_across_a_restart_and_unknown_keys_are_ignored() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        let store = SettingsStore::load(path.clone());
        store
            .apply(SettingsPatch {
                keep_in_tray: Some(true),
                ..Default::default()
            })
            .unwrap();
        store.set_last_profile("p1").unwrap();
        let again = SettingsStore::load(path.clone());
        assert!(again.get().keep_in_tray);
        assert_eq!(again.get().last_profile.as_deref(), Some("p1"));
        assert!(
            !again.get().start_engine_on_launch,
            "a switch that was not touched stays off"
        );
        std::fs::write(&path, r#"{"keep_in_tray": true, "from_the_future": 1}"#).unwrap();
        assert!(SettingsStore::load(path).get().keep_in_tray);
    }

    #[test]
    fn a_broken_file_gives_the_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(SettingsStore::load(path).get(), Settings::default());
    }

    #[test]
    fn the_patch_changes_only_what_it_names() {
        let dir = tempfile::tempdir().unwrap();
        let store = SettingsStore::load(dir.path().join("settings.json"));
        store
            .apply(SettingsPatch {
                start_engine_on_launch: Some(true),
                ..Default::default()
            })
            .unwrap();
        let s = store
            .apply(SettingsPatch {
                open_last_on_launch: Some(true),
                ..Default::default()
            })
            .unwrap();
        assert!(s.start_engine_on_launch && s.open_last_on_launch && !s.keep_in_tray);
    }

    #[cfg(unix)]
    #[test]
    fn the_file_is_private_on_unix() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        let store = SettingsStore::load(path.clone());
        store
            .apply(SettingsPatch {
                keep_in_tray: Some(true),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}
