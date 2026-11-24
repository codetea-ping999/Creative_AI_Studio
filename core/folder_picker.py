"""Open the operating system's folder chooser (Finder / Explorer) on the API host.

The Studio API only listens on loopback by default, so the machine running it is the
machine the user is sitting at. Each platform shells out to its own native dialog
instead of pulling in a GUI toolkit.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

DEFAULT_PROMPT = "Choose the folder that contains the downloaded model"
PICKER_TIMEOUT_SECONDS = 600


class FolderPickerUnavailableError(RuntimeError):
    """No native folder chooser can be opened on this machine."""


class FolderPickerFailedError(RuntimeError):
    """The folder chooser was started but failed for a reason other than Cancel."""


#: AppleScript error numbers: -128 is the user pressing Cancel, -1743 is macOS
#: refusing to let this process send Apple Events to the frontmost app.
_MACOS_USER_CANCELED = "(-128)"
_MACOS_NOT_AUTHORIZED = "(-1743)"
_MACOS_AUTOMATION_HINT = (
    "Allow it in System Settings > Privacy & Security > Automation: turn on the "
    "frontmost app (for example your browser) under the app that runs the API "
    "(Terminal, iTerm or Python), then try again."
)


def _clean_prompt(prompt: str) -> str:
    # The prompt is interpolated into AppleScript / PowerShell source, so keep it
    # to plain text.
    return "".join(ch for ch in prompt if ch.isprintable() and ch not in "\"'`$\\")[:120]


def _macos_command(prompt: str) -> list[str]:
    script = (
        "tell application (path to frontmost application as text)\n"
        f'POSIX path of (choose folder with prompt "{prompt}")\n'
        "end tell"
    )
    return ["osascript", "-e", script]


def _windows_command(prompt: str) -> list[str]:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$owner = New-Object System.Windows.Forms.Form; $owner.TopMost = $true;"
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$dialog.Description = '{prompt}'; $dialog.ShowNewFolderButton = $false;"
        "if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK)"
        " { Write-Output $dialog.SelectedPath }"
    )
    return ["powershell", "-NoProfile", "-STA", "-Command", script]


def _linux_command(prompt: str) -> list[str]:
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory", f"--title={prompt}"]
    if shutil.which("kdialog"):
        return ["kdialog", "--getexistingdirectory", ".", "--title", prompt]
    raise FolderPickerUnavailableError(
        "Install zenity or kdialog to choose a folder, or type the path instead."
    )


def build_picker_command(platform: str, prompt: str = DEFAULT_PROMPT) -> list[str]:
    cleaned = _clean_prompt(prompt) or DEFAULT_PROMPT
    if platform == "darwin":
        return _macos_command(cleaned)
    if platform.startswith("win"):
        return _windows_command(cleaned)
    return _linux_command(cleaned)


def pick_folder(prompt: str = DEFAULT_PROMPT, *, platform: str | None = None) -> str | None:
    """Show the native chooser and return the selected path, or ``None`` if cancelled."""

    platform = platform or sys.platform
    command = build_picker_command(platform, prompt)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=PICKER_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FolderPickerUnavailableError(
            f"The folder chooser is not available on this machine: {command[0]}"
        ) from exc
    except subprocess.TimeoutExpired:
        return None

    if completed.returncode != 0:
        if _is_cancel(platform, completed):
            return None
        raise FolderPickerFailedError(_failure_message(platform, completed))
    selected = completed.stdout.strip()
    if not selected:
        # PowerShell prints nothing when the dialog is cancelled.
        return None
    return selected


def _is_cancel(platform: str, completed: subprocess.CompletedProcess[str]) -> bool:
    if platform == "darwin":
        return _MACOS_USER_CANCELED in (completed.stderr or "")
    if platform.startswith("win"):
        return False
    # zenity and kdialog both exit 1 when the dialog is cancelled or closed.
    return completed.returncode == 1


def _failure_message(platform: str, completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or "").strip().splitlines()
    tail = detail[-1] if detail else f"exit status {completed.returncode}"
    if platform == "darwin":
        if _MACOS_NOT_AUTHORIZED in (completed.stderr or ""):
            return (
                "macOS did not allow the API to open the folder chooser. "
                + _MACOS_AUTOMATION_HINT
            )
        return f"The folder chooser could not be opened ({tail}). " + _MACOS_AUTOMATION_HINT
    return f"The folder chooser could not be opened ({tail})."


__all__ = [
    "DEFAULT_PROMPT",
    "FolderPickerFailedError",
    "FolderPickerUnavailableError",
    "build_picker_command",
    "pick_folder",
]
