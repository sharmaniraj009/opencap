//! opencap desktop (ADR-0010, #27): a Tauri 2 shell around the engine's own web UI.
//!
//! The launcher window (local `launcher/` page) manages connection profiles and shows each
//! engine's health. "Open" shows the engine's web UI (`<base>/ui/`) in a native window:
//! - the stored token is handed to that page through an initialization script that writes it
//!   to `sessionStorage` **only when the page's origin is the profile's origin**;
//! - the window cannot navigate to any other origin;
//! - engine windows load remote pages, so they get no Tauri IPC at all (capabilities cover the
//!   launcher window only).
//!
//! opencap is read-only towards PLCs; the desktop app adds nothing that could write one.

pub mod engine;
pub mod profiles;

use serde::Serialize;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

use profiles::{KeyringStore, ProfileInput, ProfileStore, ProfileSummary};

pub struct AppState {
    store: ProfileStore,
    profiles_path: String,
}

fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

/// Window label for a profile (Tauri labels allow `[A-Za-z0-9-/:_]`).
pub fn window_label(id: &str) -> String {
    let safe: String = id
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect();
    format!("engine-{safe}")
}

/// Script run before the engine page's own scripts: put the token where the web UI looks for
/// it, but only on the profile's origin (the window cannot leave it, this is a second guard).
pub fn injection_script(origin: &str, token: Option<&str>) -> String {
    let origin = serde_json::to_string(origin).expect("string");
    let token = token
        .map(|t| serde_json::to_string(t).expect("string"))
        .unwrap_or_else(|| "null".into());
    format!(
        "(function () {{ try {{ if (window.location.origin === {origin}) {{ var t = {token}; \
         if (t) {{ window.sessionStorage.setItem(\"opencap.token\", t); }} }} }} catch (e) {{}} }})();"
    )
}

#[derive(Serialize)]
struct AppInfo {
    version: &'static str,
    profiles_path: String,
}

#[tauri::command]
fn app_info(state: tauri::State<'_, AppState>) -> AppInfo {
    AppInfo {
        version: env!("CARGO_PKG_VERSION"),
        profiles_path: state.profiles_path.clone(),
    }
}

#[tauri::command]
fn list_profiles(state: tauri::State<'_, AppState>) -> Result<Vec<ProfileSummary>, String> {
    state.store.summaries().map_err(err)
}

#[tauri::command]
fn save_profile(
    state: tauri::State<'_, AppState>,
    input: ProfileInput,
) -> Result<ProfileSummary, String> {
    state.store.upsert(input).map_err(err)
}

#[tauri::command]
fn delete_profile(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    id: String,
) -> Result<(), String> {
    state.store.remove(&id).map_err(err)?;
    if let Some(w) = app.get_webview_window(&window_label(&id)) {
        let _ = w.close();
    }
    Ok(())
}

#[tauri::command]
async fn check_engine(
    state: tauri::State<'_, AppState>,
    id: String,
) -> Result<engine::EngineStatus, String> {
    let profile = state.store.get(&id).map_err(err)?;
    // without a keychain the engine is still checked, as if no token were stored
    let token = state.store.token(&id).unwrap_or(None);
    tauri::async_runtime::spawn_blocking(move || engine::check(&profile.base_url, token.as_deref()))
        .await
        .map_err(err)?
}

#[tauri::command]
fn open_engine(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    id: String,
) -> Result<(), String> {
    let profile = state.store.get(&id).map_err(err)?;
    // without a keychain the UI still opens; it then asks for the token itself
    let token = state.store.token(&id).unwrap_or(None);
    let label = window_label(&id);
    if let Some(window) = app.get_webview_window(&label) {
        return window.set_focus().map_err(err);
    }
    let ui: tauri::Url = format!("{}/ui/", profile.base_url).parse().map_err(err)?;
    let origin = ui.origin().ascii_serialization();
    let allowed = origin.clone();
    WebviewWindowBuilder::new(&app, label, WebviewUrl::External(ui))
        .title(format!("{} · opencap", profile.name))
        .inner_size(1280.0, 860.0)
        .min_inner_size(360.0, 480.0)
        .initialization_script(injection_script(&origin, token.as_deref()))
        .on_navigation(move |url| url.origin().ascii_serialization() == allowed)
        .build()
        .map_err(err)?;
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let path = app.path().app_config_dir()?.join("profiles.json");
            let profiles_path = path.display().to_string();
            app.manage(AppState {
                store: ProfileStore::new(path, Box::new(KeyringStore)),
                profiles_path,
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_info,
            list_profiles,
            save_profile,
            delete_profile,
            check_engine,
            open_engine
        ])
        .run(tauri::generate_context!())
        .expect("error while running opencap desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_are_safe() {
        assert_eq!(window_label("p18a2b"), "engine-p18a2b");
        assert_eq!(window_label("a b/../c"), "engine-a_b____c");
    }

    #[test]
    fn the_token_is_only_written_on_the_profile_origin() {
        let s = injection_script("http://192.0.2.10:8080", Some("tok\"en</script>"));
        assert!(s.contains("window.location.origin === \"http://192.0.2.10:8080\""));
        assert!(
            s.contains(r#""tok\"en</script>""#),
            "token must be a JSON string literal: {s}"
        );
        let none = injection_script("http://192.0.2.10:8080", None);
        assert!(none.contains("var t = null;"));
    }
}
