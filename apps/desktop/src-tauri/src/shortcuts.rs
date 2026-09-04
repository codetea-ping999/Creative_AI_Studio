//! Global shortcut for opening/focusing the Studio window.
//!
//! The shortcut is registered through the global-shortcut plugin (wired in
//! `lib.rs`). This module keeps the default accelerator and a small helper so
//! the handler in `lib.rs` stays declarative. Focus behavior lives in
//! `crate::windows::focus_main_window`.

/// Default accelerator used to open/focus the Studio window regardless of tray
/// state. `CommandOrControl` maps to Cmd on macOS and Ctrl elsewhere.
pub const DEFAULT_SHORTCUT: &str = "CommandOrControl+Shift+Space";
