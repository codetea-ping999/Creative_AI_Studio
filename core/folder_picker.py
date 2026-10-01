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

    command = build_picker_command(platform or sys.platform, prompt)
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

    # osascript exits 1 on Cancel, zenity exits 1, PowerShell prints nothing.
    if completed.returncode != 0:
        return None
    selected = completed.stdout.strip()
    if not selected:
        return None
    return selected


__all__ = [
    "DEFAULT_PROMPT",
    "FolderPickerUnavailableError",
    "build_picker_command",
    "pick_folder",
]
