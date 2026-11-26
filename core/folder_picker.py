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
#: How long the dialog may stay open. AppleScript's own Apple Event timeout
#: (120 s by default) is raised to match, and the subprocess gets a little more.
PICKER_TIMEOUT_SECONDS = 600
_SUBPROCESS_GRACE_SECONDS = 30


class FolderPickerUnavailableError(RuntimeError):
    """No native folder chooser can be opened on this machine."""


class FolderPickerFailedError(RuntimeError):
    """The folder chooser was started but failed for a reason other than Cancel."""


class FolderPickerTimeoutError(RuntimeError):
    """The folder chooser stayed open longer than ``PICKER_TIMEOUT_SECONDS``."""


#: AppleScript error numbers: -128 is the user pressing Cancel, and -1712 is the
#: Apple Event timing out (the dialog stayed open too long).
_MACOS_USER_CANCELED = "(-128)"
_MACOS_TIMED_OUT = "(-1712)"
_TIMEOUT_MESSAGE = (
    "The folder chooser was open too long and closed. Press the button again."
)


def _clean_prompt(prompt: str) -> str:
    # The prompt is interpolated into AppleScript / PowerShell source, so keep it
    # to plain text.
    return "".join(ch for ch in prompt if ch.isprintable() and ch not in "\"'`$\\")[:120]


def _macos_command(prompt: str) -> list[str]:
    # osascript shows the chooser itself and ``tell me to activate`` brings it to
    # the front. Asking the frontmost app (the browser running the Studio UI) to
    # show it instead left the chooser invisible in Safari. Not sending events to
    # another app also means macOS asks for no Automation permission.
    script = (
        f"with timeout of {PICKER_TIMEOUT_SECONDS} seconds\n"
        "tell me to activate\n"
        f'POSIX path of (choose folder with prompt "{prompt}")\n'
        "end timeout"
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
            timeout=PICKER_TIMEOUT_SECONDS + _SUBPROCESS_GRACE_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FolderPickerUnavailableError(
            f"The folder chooser is not available on this machine: {command[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FolderPickerTimeoutError(_TIMEOUT_MESSAGE) from exc

    if completed.returncode != 0:
        if _is_cancel(platform, completed):
            return None
        if platform == "darwin" and _MACOS_TIMED_OUT in (completed.stderr or ""):
            raise FolderPickerTimeoutError(_TIMEOUT_MESSAGE)
        raise FolderPickerFailedError(_failure_message(completed))
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


def _failure_message(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or "").strip().splitlines()
    tail = detail[-1] if detail else f"exit status {completed.returncode}"
    return f"The folder chooser could not be opened ({tail})."


__all__ = [
    "DEFAULT_PROMPT",
    "FolderPickerFailedError",
    "FolderPickerTimeoutError",
    "FolderPickerUnavailableError",
    "build_picker_command",
    "pick_folder",
]
