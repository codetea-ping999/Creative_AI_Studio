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

use tauri::{Manager, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

/// Label of the single Studio WebView window.
pub const MAIN_WINDOW_LABEL: &str = "main";

/// Loopback endpoint used when no runtime configuration is present.
const FALLBACK_BACKEND_ENDPOINT: &str = "http://127.0.0.1:8000";

/// Environment variable naming the Studio checkout whose root `.env` the
/// desktop shell should read (the same file `scripts/run_api_dev.sh` sources).
pub const STUDIO_ROOT_ENV: &str = "CREATIVE_AI_STUDIO_ROOT";

/// True when `dir` looks like a Creative AI Studio checkout (the directory
/// `scripts/run_api_dev.sh` runs from).
fn is_studio_root(dir: &Path) -> bool {
    dir.join("scripts").join("run_api_dev.sh").is_file() && dir.join("apps").join("api").is_dir()
}

/// Locate the `.env` file the shell reads `API_PORT` from, resolved at
/// runtime on the user's machine (never a path baked in at build time).
///
/// Precedence:
///
/// 1. `CREATIVE_AI_STUDIO_ROOT` — explicit checkout root; when set (non-empty)
///    it is authoritative and `<root>/.env` is the only candidate;
/// 2. the nearest ancestor of the running executable that is a Studio
///    checkout (a bundle built and run inside the checkout, e.g. from
///    `apps/desktop/src-tauri/target/...`);
/// 3. `<app config dir>/.env` — the per-user location for an installed app
///    (`~/Library/Application Support/com.creativeaistudio.desktop/.env` on
///    macOS);
/// 4. debug builds only: the build-time checkout (`CARGO_MANIFEST_DIR/../../..`),
///    so `cargo tauri dev` keeps following the developer's root `.env`.
///
/// Candidates 2–4 are used only when the `.env` file actually exists.
fn locate_dotenv(
    studio_root_env: Option<String>,
    current_exe: Option<PathBuf>,
    app_config_dir: Option<PathBuf>,
    debug_build_root: Option<PathBuf>,
) -> Option<PathBuf> {
    if let Some(root) = studio_root_env {
        let trimmed = root.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed).join(".env"));
        }
    }
    let exe_root = current_exe.and_then(|exe| {
        exe.ancestors()
            .skip(1)
            .find(|dir| is_studio_root(dir))
            .map(Path::to_path_buf)
    });
    [
        exe_root.map(|root| root.join(".env")),
        app_config_dir.map(|dir| dir.join(".env")),
        debug_build_root.map(|root| root.join(".env")),
    ]
    .into_iter()
    .flatten()
    .find(|candidate| candidate.is_file())
}

/// Build-time checkout root, offered as a candidate only in debug builds so a
/// release bundle never reads the build machine's checkout.
fn debug_build_root() -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .map(Path::to_path_buf)
    } else {
        None
    }
}

/// What the root `.env` says about `API_PORT`, mirroring how
/// `scripts/run_api_dev.sh` consumes it (`set -a; source .env`).
#[derive(Debug, PartialEq, Eq)]
enum DotenvPort {
    /// No `API_PORT` assignment: the inherited environment decides.
    Unset,
    /// `API_PORT=` (empty): the backend falls back to `${API_PORT:-8000}`.
    Empty,
    /// A valid, non-zero port.
    Port(u16),
    /// A value the backend could not bind; ignored here.
    Invalid,
}

/// Extract the effective `API_PORT` from the subset of dotenv syntax the
/// development scripts use (`API_PORT=NNNN`, optional leading `export `,
/// surrounding quotes, trailing ` # comment` on unquoted values). Like a
/// shell `source`, the **last** assignment wins. Comment and unrelated lines
/// are ignored.
fn dotenv_api_port(contents: &str) -> DotenvPort {
    let mut last: Option<&str> = None;
    for raw in contents.lines() {
        let trimmed = raw.trim();
        let line = trimmed
            .strip_prefix("export ")
            .map(str::trim_start)
            .unwrap_or(trimmed);
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        if key != "API_PORT" {
            continue;
        }
        last = Some(value);
    }
    let Some(value) = last else {
        return DotenvPort::Unset;
    };
    let value = value.trim();
    let value = if let Some(inner) = value
        .strip_prefix('"')
        .and_then(|rest| rest.strip_suffix('"'))
        .or_else(|| {
            value
                .strip_prefix('\'')
                .and_then(|rest| rest.strip_suffix('\''))
        }) {
        inner
    } else {
        // Unquoted: a shell treats ` #...` as a comment.
        value
            .split_once(" #")
            .map(|(head, _)| head)
            .unwrap_or(value)
            .trim()
    };
    if value.is_empty() {
        return DotenvPort::Empty;
    }
    match value.parse::<u16>() {
        Ok(port) if port != 0 => DotenvPort::Port(port),
        _ => DotenvPort::Invalid,
    }
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
/// (ADR ¶3). The port precedence matches `scripts/run_api_dev.sh`, which
/// sources the root `.env` with `set -a` *after* inheriting the environment,
/// so `.env` overrides an exported `API_PORT`:
///
/// 1. `STUDIO_BACKEND_URL` — explicit full-URL override for the desktop shell;
/// 2. `API_PORT` from the root `.env` (last assignment wins; an empty value
///    means the backend's own default 8000);
/// 3. `API_PORT` from the launch environment;
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
    match dotenv_contents.map_or(DotenvPort::Unset, dotenv_api_port) {
        DotenvPort::Port(port) => return format!("http://127.0.0.1:{port}"),
        DotenvPort::Empty => return FALLBACK_BACKEND_ENDPOINT.to_string(),
        DotenvPort::Unset | DotenvPort::Invalid => {}
    }
    if let Some(endpoint) = port_loopback_endpoint(api_port_env) {
        return endpoint;
    }
    FALLBACK_BACKEND_ENDPOINT.to_string()
}

#[tauri::command]
fn get_backend_endpoint(app: tauri::AppHandle) -> String {
    let dotenv_path = locate_dotenv(
        std::env::var(STUDIO_ROOT_ENV).ok(),
        std::env::current_exe().ok(),
        app.path().app_config_dir().ok(),
        debug_build_root(),
    );
    let dotenv = dotenv_path.and_then(|path| std::fs::read_to_string(path).ok());
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
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            None,
        ))
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
        debug_build_root, dotenv_api_port, is_studio_root, locate_dotenv, port_loopback_endpoint,
        resolve_backend_endpoint, DotenvPort, FALLBACK_BACKEND_ENDPOINT,
    };
    use std::fs;
    use std::path::PathBuf;

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

    /// Fresh scratch directory under the system temp dir.
    fn scratch(name: &str) -> PathBuf {
        // Nanosecond timestamp keeps concurrent runs apart without touching
        // the process API that check_no_backend_spawn.py forbids.
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or_default();
        let dir = std::env::temp_dir().join(format!("cas-desktop-test-{nanos}-{name}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn make_checkout(root: &PathBuf) {
        fs::create_dir_all(root.join("scripts")).unwrap();
        fs::create_dir_all(root.join("apps").join("api")).unwrap();
        fs::write(root.join("scripts").join("run_api_dev.sh"), "").unwrap();
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
    fn dotenv_wins_over_env_api_port_like_run_api_dev() {
        // run_api_dev.sh sources .env with `set -a` after inheriting the
        // environment, so the .env value is what the backend binds.
        assert_eq!(
            resolve(None, Some("8123"), Some("API_PORT=9000")),
            "http://127.0.0.1:9000"
        );
    }

    #[test]
    fn env_api_port_used_when_dotenv_has_no_assignment() {
        assert_eq!(
            resolve(None, Some("8123"), Some("WEB_PORT=5173\n# API_PORT=9000\n")),
            "http://127.0.0.1:8123"
        );
    }

    #[test]
    fn empty_dotenv_assignment_means_backend_default() {
        // `API_PORT=` in .env clears the exported value; the backend then
        // uses `${API_PORT:-8000}`.
        assert_eq!(
            resolve(None, Some("8123"), Some("API_PORT=\n")),
            FALLBACK_BACKEND_ENDPOINT
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
    fn last_dotenv_assignment_wins_like_shell_source() {
        assert_eq!(
            dotenv_api_port("API_PORT=8123\nAPI_PORT=9000\n"),
            DotenvPort::Port(9000)
        );
        assert_eq!(
            resolve(None, None, Some("API_PORT=8123\nexport API_PORT=9000\n")),
            "http://127.0.0.1:9000"
        );
    }

    #[test]
    fn invalid_dotenv_port_falls_through_to_env() {
        assert_eq!(
            resolve(None, Some("8123"), Some("API_PORT=not-a-port")),
            "http://127.0.0.1:8123"
        );
    }

    #[test]
    fn invalid_env_port_falls_back_to_default() {
        assert_eq!(
            resolve(None, Some("not-a-port"), None),
            FALLBACK_BACKEND_ENDPOINT
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
    fn dotenv_parser_accepts_export_quotes_and_inline_comment() {
        assert_eq!(
            dotenv_api_port("export API_PORT='8123'\n"),
            DotenvPort::Port(8123)
        );
        assert_eq!(
            dotenv_api_port("API_PORT=\"8123\"\n"),
            DotenvPort::Port(8123)
        );
        assert_eq!(
            dotenv_api_port("API_PORT=8123 # dev\n"),
            DotenvPort::Port(8123)
        );
        assert_eq!(dotenv_api_port("  API_PORT=8123\n"), DotenvPort::Port(8123));
    }

    #[test]
    fn dotenv_parser_ignores_comments_other_vars_zero_and_garbage() {
        assert_eq!(dotenv_api_port("# API_PORT=8123\n"), DotenvPort::Unset);
        assert_eq!(dotenv_api_port("WEB_PORT=8123\n"), DotenvPort::Unset);
        assert_eq!(dotenv_api_port("API_PORT=0\n"), DotenvPort::Invalid);
        assert_eq!(
            dotenv_api_port("API_PORT=not-a-port\n"),
            DotenvPort::Invalid
        );
        assert_eq!(dotenv_api_port("API_PORT=\n"), DotenvPort::Empty);
        assert_eq!(dotenv_api_port(""), DotenvPort::Unset);
    }

    #[test]
    fn port_loopback_rejects_zero_and_overflow() {
        assert_eq!(port_loopback_endpoint(Some(String::from("0"))), None);
        assert_eq!(port_loopback_endpoint(Some(String::from("999999"))), None);
        assert_eq!(port_loopback_endpoint(None), None);
    }

    #[test]
    fn explicit_studio_root_is_authoritative() {
        let other = scratch("explicit-other");
        fs::write(other.join(".env"), "API_PORT=9000\n").unwrap();
        assert_eq!(
            locate_dotenv(Some("/srv/studio".into()), None, Some(other.clone()), None),
            Some(PathBuf::from("/srv/studio/.env"))
        );
        // Blank override is ignored.
        assert_eq!(
            locate_dotenv(Some("  ".into()), None, Some(other.clone()), None),
            Some(other.join(".env"))
        );
    }

    #[test]
    fn executable_inside_a_checkout_uses_that_checkout() {
        let root = scratch("exe-root");
        make_checkout(&root);
        fs::write(root.join(".env"), "API_PORT=8123\n").unwrap();
        let config = scratch("exe-config");
        fs::write(config.join(".env"), "API_PORT=9000\n").unwrap();
        let exe =
            root.join("apps/desktop/src-tauri/target/release/bundle/X.app/Contents/MacOS/bin");
        assert!(is_studio_root(&root));
        assert_eq!(
            locate_dotenv(None, Some(exe), Some(config), None),
            Some(root.join(".env"))
        );
    }

    #[test]
    fn installed_app_uses_app_config_dir_not_build_checkout() {
        let config = scratch("installed-config");
        fs::write(config.join(".env"), "API_PORT=8123\n").unwrap();
        let exe = PathBuf::from("/Applications/Creative AI Studio.app/Contents/MacOS/bin");
        assert_eq!(
            locate_dotenv(None, Some(exe.clone()), Some(config.clone()), None),
            Some(config.join(".env"))
        );
        // Nothing configured: no .env at all (release builds pass no build root).
        let empty = scratch("installed-empty");
        assert_eq!(locate_dotenv(None, Some(exe), Some(empty), None), None);
    }

    #[test]
    fn debug_build_root_is_only_a_last_resort() {
        let build_root = scratch("debug-root");
        fs::write(build_root.join(".env"), "API_PORT=8123\n").unwrap();
        assert_eq!(
            locate_dotenv(None, None, None, Some(build_root.clone())),
            Some(build_root.join(".env"))
        );
        if cfg!(debug_assertions) {
            assert!(debug_build_root()
                .expect("debug build exposes the checkout")
                .join("apps")
                .join("api")
                .is_dir());
        } else {
            assert_eq!(debug_build_root(), None);
        }
    }
}
