//! Studio window lifecycle helpers shared by the tray, the global shortcut,
//! and single-instance handling.

use tauri::{AppHandle, Manager, Runtime, WebviewWindow};

use crate::MAIN_WINDOW_LABEL;

/// Return the Studio window if it exists.
pub fn main_window<R: Runtime>(app: &AppHandle<R>) -> Option<WebviewWindow<R>> {
    app.get_webview_window(MAIN_WINDOW_LABEL)
}

/// Make the Studio window visible, unminimized, and focused.
///
/// Used to reopen/restore the window from the tray, from the global shortcut,
/// and to focus the existing instance on a second launch.
pub fn focus_main_window<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    if let Some(window) = main_window(app) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
    Ok(())
}
