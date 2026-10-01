//! Global shortcut for opening/focusing the Studio window.
//!
//! The shortcut is registered after plugin initialization in `lib.rs`, in a
//! non-fatal way: a parse failure here or an OS-level registration refusal
//! there must never take down the resident shell. This module keeps the
//! default accelerator and a small parser so that path stays declarative and
//! deterministically testable. Focus behavior lives in
//! `crate::windows::focus_main_window`.

use tauri_plugin_global_shortcut::Shortcut;

/// Default accelerator used to open/focus the Studio window regardless of tray
/// state. `CommandOrControl` maps to Cmd on macOS and Ctrl elsewhere.
pub const DEFAULT_SHORTCUT: &str = "CommandOrControl+Shift+Space";

/// Parse an accelerator string into a shortcut, or `None` when the string is
/// malformed. Registration itself is OS-dependent and handled non-fatally by
/// the caller; this parser only guards the deterministic part.
pub fn parse_shortcut(value: &str) -> Option<Shortcut> {
    value.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::{parse_shortcut, DEFAULT_SHORTCUT};

    #[test]
    fn default_shortcut_parses() {
        let shortcut = parse_shortcut(DEFAULT_SHORTCUT).expect("default shortcut must parse");
        assert!(!shortcut.mods.is_empty());
    }

    #[test]
    fn empty_shortcut_is_rejected() {
        assert!(parse_shortcut("").is_none());
    }

    #[test]
    fn unknown_key_is_rejected() {
        assert!(parse_shortcut("CommandOrControl+Shift+DefinitelyNotAKey").is_none());
    }

    #[test]
    fn trailing_modifier_is_rejected() {
        assert!(parse_shortcut("CommandOrControl+Shift").is_none());
    }
}