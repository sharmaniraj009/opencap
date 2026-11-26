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
//! - with an installer, it also starts and stops the engine bundled next to it (`sidecar`, #58).
//!
//! opencap is read-only towards PLCs; the desktop app adds nothing that could write one.

pub mod engine;
pub mod profiles;
pub mod sidecar;

use serde::Serialize;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

use profiles::{KeyringStore, ProfileInput, ProfileStore, ProfileSummary};
use sidecar::{LocalEngine, LocalStatus};

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

/// The token for a profile: the keychain's, or for the bundled engine the one it was started
/// with when the keychain could not keep it.
fn token_for(state: &AppState, local: &LocalEngine, id: &str) -> Option<String> {
    let stored = state.store.token(id).unwrap_or(None);
    if stored.is_none() && id == sidecar::PROFILE_ID {
        return local.token();
    }
    stored
}

fn refuse_managed(id: &str) -> Result<(), String> {
    if id == sidecar::PROFILE_ID {
        return Err(
            "this profile belongs to the engine bundled with the app; it follows the \
                    engine's address and token by itself"
                .into(),
        );
    }
    Ok(())
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
    if let Some(id) = &input.id {
        refuse_managed(id)?;
    }
    state.store.upsert(input).map_err(err)
}

#[tauri::command]
fn delete_profile(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    id: String,
) -> Result<(), String> {
    refuse_managed(&id)?;
    state.store.remove(&id).map_err(err)?;
    if let Some(w) = app.get_webview_window(&window_label(&id)) {
        let _ = w.close();
    }
    Ok(())
}

#[tauri::command]
async fn check_engine(
    state: tauri::State<'_, AppState>,
    local: tauri::State<'_, LocalEngine>,
    id: String,
) -> Result<engine::EngineStatus, String> {
    let profile = state.store.get(&id).map_err(err)?;
    // without a keychain the engine is still checked, as if no token were stored
    let token = token_for(&state, &local, &id);
    tauri::async_runtime::spawn_blocking(move || engine::check(&profile.base_url, token.as_deref()))
        .await
        .map_err(err)?
}

#[tauri::command]
fn open_engine(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    local: tauri::State<'_, LocalEngine>,
    id: String,
) -> Result<(), String> {
    let profile = state.store.get(&id).map_err(err)?;
    // without a keychain the UI still opens; it then asks for the token itself
    let token = token_for(&state, &local, &id);
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

#[tauri::command]
fn local_engine_status(local: tauri::State<'_, LocalEngine>) -> LocalStatus {
    local.status()
}

#[tauri::command]
async fn start_local_engine(app: tauri::AppHandle) -> Result<LocalStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let local = app.state::<LocalEngine>();
        local.start(|url, token| {
            state
                .store
                .set_managed(sidecar::PROFILE_ID, sidecar::PROFILE_NAME, url, token)
                .unwrap_or_else(|e| Some(format!("Could not save the engine's profile: {e}")))
        })
    })
    .await
    .map_err(err)?
}

#[tauri::command]
async fn stop_local_engine(app: tauri::AppHandle) -> Result<LocalStatus, String> {
    tauri::async_runtime::spawn_blocking(move || app.state::<LocalEngine>().stop())
        .await
        .map_err(err)?
}

/// `opencap-desktop --check-bundled-engine [report-file]`: the installer smoke test (#58). Uses
/// the engine next to the installed app, a temporary config directory and an in-memory token;
/// touches neither the user's profiles nor the keychain.
pub fn check_bundled_engine(report: Option<&str>) -> i32 {
    let tmp = std::env::temp_dir().join(format!("opencap-desktop-check-{}", std::process::id()));
    let local = LocalEngine::new(sidecar::Layout::new(tmp.clone()), sidecar::engine_binary());
    let result = local.start(|_, _| None).and_then(|st| {
        let url = st.base_url.clone().unwrap_or_default();
        let health = engine::check(&url, local.token().as_deref())?;
        let ok = matches!(health.auth, engine::Auth::Ok { .. });
        let stopped = local.stop()?;
        if !ok || stopped.running {
            return Err(format!(
                "unexpected: auth {:?}, running after stop: {}",
                health.auth, stopped.running
            ));
        }
        Ok(format!(
            "ok: bundled engine {} started on {url}, /health {:?} with its token, stopped",
            st.binary, health.version
        ))
    });
    let _ = std::fs::remove_dir_all(&tmp);
    let (code, text) = match result {
        Ok(t) => (0, t),
        Err(e) => (1, format!("failed: {e}")),
    };
    println!("{text}");
    if let Some(path) = report {
        let _ = std::fs::write(path, format!("{text}\n"));
    }
    code
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
            // the bundled engine's config and outbox: local app data, kept across upgrades
            let engine_root = app.path().app_local_data_dir()?.join("engine");
            app.manage(LocalEngine::new(
                sidecar::Layout::new(engine_root),
                sidecar::engine_binary(),
            ));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_info,
            list_profiles,
            save_profile,
            delete_profile,
            check_engine,
            open_engine,
            local_engine_status,
            start_local_engine,
            stop_local_engine
        ])
        .build(tauri::generate_context!())
        .expect("error while building opencap desktop")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                // never leave the bundled engine running without the app that manages it
                let _ = app.state::<LocalEngine>().stop();
            }
        });
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
