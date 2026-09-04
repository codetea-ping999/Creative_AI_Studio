//! Creative AI Studio desktop shell.
//!
//! This Rust process is a thin resident shell. It owns only desktop concerns:
//! system tray, global shortcut, single-instance focus, autostart
//! configuration, and Studio window lifecycle.
//!
//! Hard invariant: this process must never spawn Python/FastAPI workers,
//! initialize CUDA, or load AI model runtimes. See
//! `docs/desktop/architecture-decision.md`.

pub mod shortcuts;
pub mod tray;
pub mod windows;

use tauri::{Manager, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};

/// Label of the single Studio WebView window.
pub const MAIN_WINDOW_LABEL: &str = "main";

/// Set the OS autostart state for the Studio application.
///
/// Autostart defaults to disabled and is only changed by explicit user
/// request. Enabling autostart never implies starting Python, FastAPI, CUDA,
/// or model runtimes; this shell remains a UI/resident-wrapper only.
#[tauri::command]
fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let autostart = app.autolaunch();
    if enabled {
        autostart.enable().map_err(|err| err.to_string())
    } else {
        autostart.disable().map_err(|err| err.to_string())
    }
}

/// Report whether OS autostart is currently enabled for this application.
#[tauri::command]
fn is_autostart_enabled(app: tauri::AppHandle) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false)
}

/// Build the global-shortcut plugin, or `None` when the platform refuses to
/// register the shortcut. The shell must stay usable even without the
/// shortcut, so a registration failure is logged rather than fatal.
fn build_shortcut_plugin() -> Option<tauri::plugin::TauriPlugin<tauri::Wry>> {
    let builder = tauri_plugin_global_shortcut::Builder::new().with_handler(
        |app, _shortcut, event| {
            if event.state() == tauri_plugin_global_shortcut::ShortcutState::Pressed {
                let _ = windows::focus_main_window(app);
            }
        },
    );

    let builder = match builder.with_shortcuts([shortcuts::DEFAULT_SHORTCUT]) {
        Ok(builder) => builder,
        Err(err) => {
            eprintln!("desktop: failed to configure global shortcut: {err}");
            return None;
        }
    };

    Some(builder.build())
}

fn build_app() -> tauri::Builder<tauri::Wry> {
    let mut builder = tauri::Builder::default()
        // A second instance focuses the already-running Studio window instead
        // of creating a duplicate resident process.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = windows::focus_main_window(app);
        }))
        // Autostart is registered but disabled by default (`set_autostart`).
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None));

    if let Some(plugin) = build_shortcut_plugin() {
        builder = builder.plugin(plugin);
    }

    builder
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    build_app()
        .setup(|app| {
            tray::create_tray(app.handle())?;
            Ok(())
        })
        // Close-to-tray for the Studio window only: closing hides the WebView
        // to the tray instead of terminating the application. This is scoped
        // to the `main` label so auxiliary windows (if any later appear) close
        // normally.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == MAIN_WINDOW_LABEL {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            set_autostart,
            is_autostart_enabled
        ])
        .run(tauri::generate_context!())
        .expect("error while running Creative AI Studio desktop shell");
}
