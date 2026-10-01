//! System tray for the Creative AI Studio desktop shell.
//!
//! The tray provides: a left-click to reopen/focus the Studio window, menu
//! actions to show/hide/quit, and a tooltip. It is the primary way to restore
//! the window after close-to-tray, and it exposes the user control for the OS
//! autostart setting (disabled by default, changed only on explicit request).

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_autostart::ManagerExt;

use crate::MAIN_WINDOW_LABEL;

const TRAY_ID: &str = "creative-ai-studio";
const MENU_SHOW: &str = "show";
const MENU_HIDE: &str = "hide";
const MENU_AUTOSTART: &str = "autostart";
const MENU_QUIT: &str = "quit";

/// Current OS autostart state for this application (defaults to disabled).
fn autostart_enabled(app: &AppHandle<Wry>) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false)
}

/// Rebuild the tray context menu so the autostart checkmark reflects the live
/// OS state after a toggle.
fn build_tray_menu(app: &AppHandle<Wry>) -> tauri::Result<Menu<Wry>> {
    let show = MenuItem::with_id(app, MENU_SHOW, "Show Studio", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, MENU_HIDE, "Hide Studio", true, None::<&str>)?;
    let autostart = CheckMenuItem::with_id(
        app,
        MENU_AUTOSTART,
        "Start on login",
        true,
        autostart_enabled(app),
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(
        app,
        MENU_QUIT,
        "Quit Creative AI Studio",
        true,
        None::<&str>,
    )?;
    Menu::with_items(
        app,
        &[
            &show,
            &hide,
            &PredefinedMenuItem::separator(app)?,
            &autostart,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )
}

/// Build and attach the tray icon with its context menu.
pub fn create_tray(app: &AppHandle<Wry>) -> tauri::Result<()> {
    let menu = build_tray_menu(app)?;

    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip("Creative AI Studio")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            MENU_SHOW => {
                let _ = crate::windows::focus_main_window(app);
            }
            MENU_HIDE => {
                if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
                    let _ = window.hide();
                }
            }
            MENU_AUTOSTART => {
                // Toggle only on explicit user action; flipping the flag never
                // implies starting the API/model runtime.
                let current = autostart_enabled(app);
                let _ = crate::set_autostart(app.clone(), !current);
                if let Some(tray) = app.tray_by_id(TRAY_ID) {
                    if let Ok(menu) = build_tray_menu(app) {
                        let _ = tray.set_menu(Some(menu));
                    }
                }
            }
            MENU_QUIT => {
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let _ = crate::windows::focus_main_window(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }

    builder.build(app)?;
    Ok(())
}
