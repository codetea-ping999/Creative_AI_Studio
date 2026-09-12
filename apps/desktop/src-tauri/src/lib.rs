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

use std::path::{Path, PathBuf};

use tauri::WindowEvent;
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

/// Label of the single Studio WebView window.
pub const MAIN_WINDOW_LABEL: &str = "main";

/// Loopback endpoint used when no runtime configuration is present.
const FALLBACK_BACKEND_ENDPOINT: &str = "http://127.0.0.1:8000";

/// Project root at build time (three ancestors above `CARGO_MANIFEST_DIR`). It
/// locates the same root `.env` that the development scripts source, so the
/// packaged UI follows the same runtime configuration without rebuilding the
/// bundle.
fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from(""))
}

/// Extract `API_PORT` from the subset of dotenv syntax the development scripts
/// use (`API_PORT=NNNN`, optional leading `export `, surrounding quotes).
/// Comment and unrelated lines are ignored; port `0` and non-numeric values are
/// rejected.
fn dotenv_api_port(contents: &str) -> Option<u16> {
    contents.lines().find_map(|raw| {
        let line = raw
            .trim()
            .strip_prefix("export ")
            .map(str::trim)
            .unwrap_or(raw.trim());
        let (key, raw_port) = line.split_once('=')?;
        if key.trim() != "API_PORT" {
            return None;
        }
        let port = raw_port.trim().trim_matches(['"', '\'']);
        port.parse::<u16>().ok().filter(|port| *port != 0)
    })
}

/// Build a loopback endpoint from a port string, or `None` when it is not a
/// valid service port (invalid values fall through to the next precedence
/// level).
fn port_loopback_endpoint(port_value: Option<String>) -> Option<String> {
    port_value
        .and_then(|value| value.trim().parse::<u16>().ok())
        .filter(|port| *port != 0)
        .map(|port| format!("http://127.0.0.1:{port}"))
}

/// Resolve the backend endpoint from Tauri-managed runtime configuration.
///
/// The desktop bundle must not depend solely on build-time `VITE_API_BASE_URL`
/// (ADR ¶3). Precedence:
///
/// 1. `STUDIO_BACKEND_URL` — explicit full-URL override for the desktop shell;
/// 2. `API_PORT` from the launch environment — the existing development
///    backend knob (e.g. `API_PORT=8123` with `scripts/run_api_dev.sh`);
/// 3. `API_PORT` from the project root `.env` — the configuration file the
///    development scripts source, so a normally-launched packaged app follows
///    the same set port without rebuilding or exporting overrides;
/// 4. loopback default `http://127.0.0.1:8000`.
fn resolve_backend_endpoint(
    studio_backend_url: Option<String>,
    api_port_env: Option<String>,
    dotenv_contents: Option<&str>,
) -> String {
    if let Some(value) = studio_backend_url {
        let trimmed = value.trim().to_string();
        if !trimmed.is_empty() {
            return trimmed;
        }
    }
    if let Some(endpoint) = port_loopback_endpoint(api_port_env) {
        return endpoint;
    }
    if let Some(port) = dotenv_contents.and_then(dotenv_api_port) {
        return format!("http://127.0.0.1:{port}");
    }
    FALLBACK_BACKEND_ENDPOINT.to_string()
}

#[tauri::command]
fn get_backend_endpoint() -> String {
    let dotenv = std::fs::read_to_string(project_root().join(".env")).ok();
    resolve_backend_endpoint(
        std::env::var("STUDIO_BACKEND_URL").ok(),
        std::env::var("API_PORT").ok(),
        dotenv.as_deref(),
    )
}

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

fn build_app() -> tauri::Builder<tauri::Wry> {
    tauri::Builder::default()
        // A second instance focuses the already-running Studio window instead
        // of creating a duplicate resident process.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = windows::focus_main_window(app);
        }))
        // Autostart is registered but disabled by default (`set_autostart`).
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        // The global-shortcut plugin is installed without shortcuts so plugin
        // setup can never fail on registration; the shortcut is registered in
        // `setup` below in a non-fatal way.
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    build_app()
        .setup(|app| {
            tray::create_tray(app.handle())?;
            // Register the global shortcut after plugin initialization. A
            // refusal (for example another application holding the platform
            // key combination) is logged but never fatal: shell, tray, Studio
            // window, and backend connection stay usable without it. Only a
            // malformed accelerator is rejected here and tested up front.
            if let Err(err) = app.global_shortcut().on_shortcut(
                shortcuts::DEFAULT_SHORTCUT,
                |app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        let _ = windows::focus_main_window(app);
                    }
                },
            ) {
                eprintln!("desktop: global shortcut registration failed (non-fatal): {err}");
            }
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
            is_autostart_enabled,
            get_backend_endpoint
        ])
        .run(tauri::generate_context!())
        .expect("error while running Creative AI Studio desktop shell");
}

#[cfg(test)]
mod tests {
    use super::{
        dotenv_api_port, port_loopback_endpoint, project_root, resolve_backend_endpoint,
        FALLBACK_BACKEND_ENDPOINT,
    };

    fn resolve(
        studio_backend_url: Option<&str>,
        api_port_env: Option<&str>,
        dotenv_contents: Option<&str>,
    ) -> String {
        resolve_backend_endpoint(
            studio_backend_url.map(String::from),
            api_port_env.map(String::from),
            dotenv_contents,
        )
    }

    #[test]
    fn falls_back_to_loopback_default_without_override() {
        assert_eq!(resolve(None, None, None), FALLBACK_BACKEND_ENDPOINT);
    }

    #[test]
    fn empty_studio_backend_url_falls_back() {
        assert_eq!(resolve(Some("  "), None, None), FALLBACK_BACKEND_ENDPOINT);
    }

    #[test]
    fn studio_backend_url_is_trimmed_and_used() {
        assert_eq!(
            resolve(Some("  http://127.0.0.1:8123  "), Some("9000"), None),
            "http://127.0.0.1:8123"
        );
    }

    #[test]
    fn env_api_port_builds_loopback_endpoint() {
        assert_eq!(resolve(None, Some("8123"), None), "http://127.0.0.1:8123");
    }

    #[test]
    fn env_api_port_wins_over_dotenv() {
        assert_eq!(
            resolve(None, Some("8123"), Some("API_PORT=9000")),
            "http://127.0.0.1:8123"
        );
    }

    #[test]
    fn dotenv_api_port_used_when_no_environment_override() {
        assert_eq!(
            resolve(
                None,
                None,
                Some("API_PORT=8123\n# API_PORT=9999\nWEB_PORT=5173\n")
            ),
            "http://127.0.0.1:8123"
        );
    }

    #[test]
    fn invalid_env_port_falls_through_to_dotenv() {
        assert_eq!(
            resolve(None, Some("not-a-port"), Some("API_PORT=8123")),
            "http://127.0.0.1:8123"
        );
    }

    #[test]
    fn studio_backend_url_beats_every_port_source() {
        assert_eq!(
            resolve(
                Some("http://10.0.0.5:9999"),
                Some("8123"),
                Some("API_PORT=9000")
            ),
            "http://10.0.0.5:9999"
        );
    }

    #[test]
    fn dotenv_parser_accepts_export_and_quotes() {
        assert_eq!(dotenv_api_port("export API_PORT='8123'\n"), Some(8123));
        assert_eq!(dotenv_api_port("API_PORT=\"8123\"\n"), Some(8123));
        assert_eq!(dotenv_api_port("API_PORT= 8123 \n"), Some(8123));
    }

    #[test]
    fn dotenv_parser_ignores_comments_other_vars_zero_and_garbage() {
        assert_eq!(dotenv_api_port("# API_PORT=8123\n"), None);
        assert_eq!(dotenv_api_port("WEB_PORT=8123\n"), None);
        assert_eq!(dotenv_api_port("API_PORT=0\n"), None);
        assert_eq!(dotenv_api_port("API_PORT=not-a-port\n"), None);
        assert_eq!(dotenv_api_port(""), None);
    }

    #[test]
    fn port_loopback_rejects_zero_and_overflow() {
        assert_eq!(port_loopback_endpoint(Some(String::from("0"))), None);
        assert_eq!(port_loopback_endpoint(Some(String::from("999999"))), None);
        assert_eq!(port_loopback_endpoint(None), None);
    }

    #[test]
    fn project_root_located_above_manifest_dir() {
        assert!(project_root().join("apps").join("api").is_dir());
    }
}
