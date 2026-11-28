//! The tray icon (Linux status area, Windows notification area): a small menu for the things one
//! reaches for while the launcher window is closed. It is built and handled in Rust, so it adds no
//! Tauri permission and the engine windows still get no IPC. Nothing in it writes to a PLC.

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};

use crate::sidecar::LocalEngine;
use crate::AppState;

const SHOW: &str = "show";
const OPEN_LAST: &str = "open_last";
const START: &str = "start_engine";
const STOP: &str = "stop_engine";
const QUIT: &str = "quit";

/// Bring the launcher window back.
pub(crate) fn show_launcher(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

pub(crate) fn build(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, SHOW, "Show opencap", true, None::<&str>)?;
    let open_last = MenuItem::with_id(
        app,
        OPEN_LAST,
        "Open the engine opened last",
        true,
        None::<&str>,
    )?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit = MenuItem::with_id(
        app,
        QUIT,
        "Quit opencap (stops the bundled engine)",
        true,
        None::<&str>,
    )?;
    let bundled = app.state::<LocalEngine>().status().available;
    let menu = if bundled {
        let start = MenuItem::with_id(app, START, "Start the bundled engine", true, None::<&str>)?;
        let stop = MenuItem::with_id(app, STOP, "Stop the bundled engine", true, None::<&str>)?;
        let sep2 = PredefinedMenuItem::separator(app)?;
        Menu::with_items(app, &[&show, &open_last, &sep, &start, &stop, &sep2, &quit])?
    } else {
        Menu::with_items(app, &[&show, &open_last, &sep, &quit])?
    };
    let mut builder = TrayIconBuilder::with_id("opencap")
        .tooltip("opencap")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            SHOW => show_launcher(app),
            OPEN_LAST => {
                let last = app.state::<AppState>().settings.get().last_profile;
                match last {
                    Some(id) => {
                        if let Err(e) = crate::open_profile(app, &id) {
                            eprintln!("opencap desktop: {e}");
                        }
                    }
                    None => show_launcher(app),
                }
            }
            START => {
                let app = app.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    if let Err(e) = crate::start_bundled(&app) {
                        eprintln!("opencap desktop: {e}");
                    }
                });
            }
            STOP => {
                let app = app.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    if let Err(e) = app.state::<LocalEngine>().stop() {
                        eprintln!("opencap desktop: {e}");
                    }
                });
            }
            QUIT => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_launcher(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}
