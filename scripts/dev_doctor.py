#!/usr/bin/env python3
"""Read-only development environment diagnostics.

All external calls use a fixed argument vector and short timeout.  The doctor
does not install, bootstrap, clean, prune, start services, or download models.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import Any, Callable, Iterable, Sequence

# Imports below must not leave __pycache__ files in a fresh checkout.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.model_readiness import evaluate_manifest_payload
from scripts.check_local_setup import (
    MIN_NODE_20_VERSION,
    MIN_NODE_22_VERSION,
    MIN_PYTHON_VERSION,
)


SCHEMA_VERSION = 1
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
STATUSES = frozenset({PASS, WARN, FAIL})
COMMAND_TIMEOUT_SECONDS = 5
SAFE_ENVIRONMENT_KEYS = (
    "API_HOST", "ALLOW_UNSAFE_API_BIND", "API_RELOAD", "DB_PATH",
    "MODELS_ROOT", "MODELS_MANIFEST_ROOT", "OUTPUT_DIR", "LOG_LEVEL",
)
SENSITIVE_ENV_RE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|COOKIE)", re.I)
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class DoctorCheck:
    """One stable, redacted diagnostic result."""

    name: str
    status: str
    summary: str
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": list(self.details),
        }


@dataclass(frozen=True)
class CommandResult:
    """A captured result whose raw output is not emitted by the doctor."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    unavailable: bool = False
    timed_out: bool = False


CommandRunner = Callable[[Sequence[str]], CommandResult]


def _check(name: str, status: str, summary: str, *details: str) -> DoctorCheck:
    if status not in STATUSES:
        raise ValueError(f"Unsupported doctor status: {status}")
    return DoctorCheck(name, status, summary, tuple(details))


def _version_text(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Extract a version without retaining arbitrary command output."""

    match = VERSION_RE.search(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def is_supported_python(version: tuple[int, int, int]) -> bool:
    return version >= (*MIN_PYTHON_VERSION, 0)


def is_supported_node(version: tuple[int, int, int]) -> bool:
    return (version[0] == 20 and version >= MIN_NODE_20_VERSION) or version >= MIN_NODE_22_VERSION


def parse_worktree_count(output: str) -> int:
    return sum(1 for line in output.splitlines() if line.startswith("worktree "))


def parse_dotenv_assignments(content: str) -> dict[str, str]:
    """Parse local dotenv input; callers must never render values."""

    assignments: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if ENVIRONMENT_NAME_RE.fullmatch(key):
            assignments[key] = value.strip()
    return assignments


def _safe_environment_key(name: str) -> bool:
    return (
        bool(ENVIRONMENT_NAME_RE.fullmatch(name))
        and not SENSITIVE_ENV_RE.search(name)
        and not name.endswith("_PORT")
    )


def parse_dotenv_keys(content: str) -> frozenset[str]:
    """Return safe names only; values, secret names, and port names stay hidden."""

    return frozenset(key for key in parse_dotenv_assignments(content) if _safe_environment_key(key))


def run_read_only(argv: Sequence[str], *, cwd: Path) -> CommandResult:
    """Run one fixed-argv diagnostic command without shell evaluation."""

    try:
        environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"} if argv[0] == "git" else None
        completed = subprocess.run(
            list(argv), cwd=cwd, check=False, capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS, shell=False, env=environment,
        )
    except FileNotFoundError:
        return CommandResult(None, unavailable=True)
    except subprocess.TimeoutExpired:
        return CommandResult(None, timed_out=True)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _result_problem(result: CommandResult) -> str | None:
    if result.unavailable:
        return "is not available on PATH"
    if result.timed_out:
        return f"did not finish within {COMMAND_TIMEOUT_SECONDS} seconds"
    if result.returncode != 0:
        return "returned a non-zero status"
    return None


def _version_check(
    name: str,
    result: CommandResult,
    *,
    supported: Callable[[tuple[int, int, int]], bool] | None = None,
    requirement: str | None = None,
) -> DoctorCheck:
    problem = _result_problem(result)
    if problem:
        return _check(name, FAIL, f"version check {problem}")
    version = parse_version(result.stdout)
    if version is None:
        return _check(name, FAIL, "version output could not be parsed")
    if supported is not None and not supported(version):
        return _check(name, FAIL, f"installed version is unsupported (requires {requirement})")
    return _check(name, PASS, "version is supported" if supported else "version is available")


def _git_checks(run: CommandRunner) -> list[DoctorCheck]:
    checks = [_version_check("Git", run(("git", "--version")))]
    branch = run(("git", "branch", "--show-current"))
    problem = _result_problem(branch)
    checks.append(
        _check("Git branch", FAIL, f"branch check {problem}") if problem else
        _check("Git branch", PASS, "a branch is checked out") if branch.stdout.strip() else
        _check("Git branch", WARN, "HEAD is detached")
    )
    head = run(("git", "rev-parse", "--short", "HEAD"))
    problem = _result_problem(head)
    head_value = head.stdout.strip()
    checks.append(
        _check("Git HEAD", FAIL, f"HEAD check {problem}") if problem else
        _check("Git HEAD", PASS, f"HEAD {head_value}")
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", head_value) else
        _check("Git HEAD", FAIL, "HEAD output is not a commit hash")
    )
    status = run(("git", "status", "--porcelain=v1"))
    problem = _result_problem(status)
    checks.append(
        _check("Git working tree", FAIL, f"status check {problem}") if problem else
        _check("Git working tree", WARN, "working tree has uncommitted changes")
        if status.stdout.strip()
        else
        _check("Git working tree", PASS, "working tree is clean")
    )
    worktrees = run(("git", "worktree", "list", "--porcelain"))
    problem = _result_problem(worktrees)
    count = parse_worktree_count(worktrees.stdout) if problem is None else 0
    checks.append(
        _check("Git worktrees", FAIL, f"worktree list {problem}") if problem else
        _check("Git worktrees", PASS if count else FAIL, f"{count} worktree(s) reported")
    )
    prunable = run(("git", "worktree", "prune", "--dry-run"))
    problem = _result_problem(prunable)
    checks.append(
        _check("Git prunable worktrees", FAIL, f"dry-run check {problem}") if problem else
        _check("Git prunable worktrees", WARN, "prunable worktree metadata found")
        if prunable.stdout.strip() or prunable.stderr.strip() else
        _check("Git prunable worktrees", PASS, "no prunable worktree metadata")
    )
    return checks


def _venv_python(root: Path) -> Path:
    candidates = (root / "venv" / "bin" / "python", root / "venv" / "Scripts" / "python.exe")
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def _python_checks(root: Path, run: CommandRunner) -> list[DoctorCheck]:
    executable = _venv_python(root)
    system_version = tuple(sys.version_info[:3])
    system_check = _check(
        "System Python",
        PASS if is_supported_python(system_version) else FAIL,
        "system Python is supported"
        if is_supported_python(system_version)
        else "system Python is unsupported",
    )
    if not executable.is_file():
        return [
            system_check,
            _check(
                "Python virtual environment",
                FAIL,
                "repository venv Python executable is missing",
            ),
            _check("Python dependencies", WARN, "not checked because repository venv is missing"),
            _check(
                "Python dependency health",
                WARN,
                "not checked because repository venv is missing",
            ),
            _check(
                "Python runtime imports",
                WARN,
                "not checked because repository venv is missing",
            ),
        ]
    requirement = f"Python {_version_text(MIN_PYTHON_VERSION)}+"
    version = _version_check(
        "Python virtual environment", run((str(executable), "--version")),
        supported=is_supported_python, requirement=requirement,
    )
    pip = _result_problem(run((str(executable), "-m", "pip", "--version")))
    pip_check = _check(
        "Python dependencies",
        FAIL if pip else PASS,
        f"pip check {pip}" if pip else "pip is available in the repository venv",
    )
    dependency_problem = _result_problem(run((str(executable), "-m", "pip", "check")))
    dependency_check = _check(
        "Python dependency health",
        FAIL if dependency_problem else PASS,
        f"pip check {dependency_problem}"
        if dependency_problem
        else "pip check reports no dependency conflicts",
    )
    imports = run(
        (str(executable), "-B", "-c", "import fastapi, pydantic; print('runtime-import-ok')")
    )
    import_problem = _result_problem(imports)
    imports_ok = import_problem is None and imports.stdout.strip() == "runtime-import-ok"
    import_check = _check(
        "Python runtime imports", PASS if imports_ok else FAIL,
        "FastAPI and Pydantic import successfully" if imports_ok else
        f"runtime import check {import_problem or 'returned an unexpected result'}",
    )
    return [system_check, version, pip_check, dependency_check, import_check]


def _node_checks(root: Path, run: CommandRunner) -> list[DoctorCheck]:
    requirement = (
        f"Node {_version_text(MIN_NODE_20_VERSION)}+ on Node 20 or "
        f"{_version_text(MIN_NODE_22_VERSION)}+ on Node 22+"
    )
    checks = [
        _version_check(
            "Node.js",
            run(("node", "--version")),
            supported=is_supported_node,
            requirement=requirement,
        ),
        _version_check("npm", run(("npm", "--version"))),
    ]
    if not (root / "apps" / "web" / "node_modules").is_dir():
        checks.append(_check("Web dependencies", FAIL, "apps/web/node_modules is missing"))
        return checks
    listing = run(("npm", "--prefix", str(root / "apps" / "web"), "ls", "--depth=0", "--json"))
    problem = _result_problem(listing)
    if problem:
        checks.append(_check("Web dependencies", FAIL, f"npm dependency check {problem}"))
        return checks
    try:
        payload = json.loads(listing.stdout)
    except json.JSONDecodeError:
        checks.append(_check("Web dependencies", FAIL, "npm dependency output is not valid JSON"))
        return checks
    problems = payload.get("problems")
    checks.append(
        _check("Web dependencies", FAIL, "npm reports dependency problems")
        if isinstance(problems, list) and problems else
        _check("Web dependencies", PASS, "apps/web dependencies are installed")
    )
    return checks


def _rust_msrv(cargo_toml: Path) -> tuple[int, int, int] | None:
    try:
        content = cargo_toml.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = re.search(r'^rust-version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    return parse_version(match.group(1)) if match else None


def _desktop_checks(root: Path, run: CommandRunner) -> list[DoctorCheck]:
    cargo_toml = root / "apps" / "desktop" / "src-tauri" / "Cargo.toml"
    if not cargo_toml.is_file():
        return [_check("Desktop toolchain", PASS, "desktop shell is not configured")]
    msrv = _rust_msrv(cargo_toml)
    cargo_check = _version_check("Cargo", run(("cargo", "--version")))
    rustc = run(("rustc", "--version"))
    problem = _result_problem(rustc)
    version = parse_version(rustc.stdout) if problem is None else None
    rustc_check = (
        _check("rustc", FAIL, f"version check {problem}") if problem else
        _check("rustc", FAIL, "rustc version or declared MSRV could not be parsed")
        if version is None or msrv is None else
        _check("rustc", FAIL, "rustc is older than the desktop shell MSRV") if version < msrv else
        _check("rustc", PASS, "rustc satisfies the desktop shell MSRV")
    )
    tauri_problem = _result_problem(run(("cargo", "tauri", "--version")))
    tauri_check = _check(
        "Tauri",
        WARN if tauri_problem else PASS,
        f"Tauri check {tauri_problem}" if tauri_problem else "cargo tauri is available",
    )
    if sys.platform != "darwin":
        xcode_check = _check("Xcode", PASS, f"not required on {sys.platform}")
    else:
        xcode_problem = _result_problem(run(("xcodebuild", "-version")))
        xcode_check = _check(
            "Xcode",
            FAIL if xcode_problem else PASS,
            f"Xcode check {xcode_problem}"
            if xcode_problem
            else "Xcode command line tools are available",
        )
    return [cargo_check, rustc_check, tauri_check, xcode_check]


def _read_dotenv(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    try:
        return parse_dotenv_assignments(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _config_value(name: str, actual: dict[str, str], defaults: dict[str, str]) -> str:
    return os.environ.get(name, actual.get(name, defaults.get(name, ""))).strip()


def _is_loopback_host(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _valid_port(value: str) -> bool:
    try:
        return 1 <= int(value) <= 65535 and value.strip() == str(int(value))
    except ValueError:
        return False


def _port_is_listening(host: str, port: int) -> bool | None:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return None


def _environment_checks(root: Path) -> list[DoctorCheck]:
    defaults = _read_dotenv(root / ".env.example")
    actual = _read_dotenv(root / ".env")
    if defaults is None:
        return [_check("Environment template", FAIL, ".env.example could not be read")]
    if actual is None:
        env_check = _check("Environment template", WARN, ".env is missing; defaults are in use")
        actual = {}
    else:
        missing = set(defaults).difference(actual)
        env_check = _check(
            "Environment template", WARN if missing else PASS,
            f"{len(missing)} .env.example key(s) are absent from .env"
            if missing
            else ".env includes every .env.example key",
            "secret and port names and values are intentionally not reported",
        )
    present = tuple(key for key in SAFE_ENVIRONMENT_KEYS if key in actual or key in os.environ)
    key_check = _check(
        "Environment keys", PASS if present else WARN,
        f"{len(present)} safe project configuration key(s) are present",
        "secret and port names and values are intentionally not reported",
    )
    host = _config_value("API_HOST", actual, defaults)
    unsafe_bind = _config_value("ALLOW_UNSAFE_API_BIND", actual, defaults).lower() in {"1", "true"}
    host_check = (
        _check("API host", PASS, "API host is loopback-only") if _is_loopback_host(host) else
        _check("API host", WARN, "non-loopback API host is explicitly allowed") if unsafe_bind else
        _check("API host", FAIL, "API host is not loopback-only")
    )
    ports = (
        ("API", _config_value("API_PORT", actual, defaults)),
        ("Web", _config_value("WEB_PORT", actual, defaults)),
    )
    port_checks = [
        _check(
            f"{name} port",
            PASS if _valid_port(value) else FAIL,
            "port is valid" if _valid_port(value) else "port is invalid",
        )
        for name, value in ports
    ]
    listener_checks: list[DoctorCheck] = []
    if _is_loopback_host(host):
        for name, value in ports:
            if not _valid_port(value):
                listener_checks.append(
                    _check(f"{name} port listener", WARN, "not checked because port is invalid")
                )
                continue
            listening = _port_is_listening(host, int(value))
            listener_checks.append(
                _check(f"{name} port listener", WARN, "a loopback listener is already present")
                if listening is True
                else _check(f"{name} port listener", PASS, "no loopback listener is present")
                if listening is False
                else
                _check(f"{name} port listener", WARN, "listener state could not be determined")
            )
    else:
        listener_checks.append(
            _check("Port listeners", WARN, "not checked for a non-loopback API host")
        )
    return [env_check, key_check, host_check, *port_checks, *listener_checks]


def _models_root(root: Path, actual: dict[str, str], defaults: dict[str, str]) -> Path:
    value = _config_value("MODELS_ROOT", actual, defaults) or "models"
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def _manifest_root(root: Path, actual: dict[str, str], defaults: dict[str, str]) -> Path:
    value = _config_value("MODELS_MANIFEST_ROOT", actual, defaults)
    if value:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else root / candidate
    return _models_root(root, actual, defaults) / "manifests"


def _broken_symlink_count(root: Path) -> int | None:
    try:
        return sum(1 for path in root.rglob("*") if path.is_symlink() and not path.exists())
    except OSError:
        return None


def _model_checks(root: Path) -> list[DoctorCheck]:
    actual = _read_dotenv(root / ".env") or {}
    defaults = _read_dotenv(root / ".env.example") or {}
    models_root = _models_root(root, actual, defaults)
    broken = _broken_symlink_count(models_root) if models_root.is_dir() else 0
    symlinks = _check(
        "Model symlinks",
        FAIL if broken else PASS,
        f"{broken or 0} broken model symlink(s) found"
        if broken
        else "no broken model symlinks found",
    )
    manifest_root = _manifest_root(root, actual, defaults)
    if not manifest_root.is_dir():
        return [
            symlinks,
            _check("Model manifests", FAIL, "model manifest directory is missing"),
            _check("Model readiness", WARN, "not evaluated because manifests are unavailable"),
        ]
    try:
        paths = tuple(sorted(manifest_root.rglob("*.json")))
    except OSError:
        paths = ()
    if not paths:
        return [
            symlinks,
            _check("Model manifests", FAIL, "no model manifest files were found"),
            _check("Model readiness", WARN, "not evaluated because manifests are unavailable"),
        ]
    payloads: list[dict[str, Any]] = []
    invalid = 0
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            invalid += 1
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
        else:
            invalid += 1
    manifests = _check(
        "Model manifests",
        FAIL if invalid else PASS,
        f"{len(payloads)} manifest file(s) are readable; {invalid} invalid"
        if invalid
        else f"{len(payloads)} manifest file(s) are readable",
    )
    ready = unavailable = 0
    for payload in payloads:
        if payload.get("enabled") is False:
            continue
        try:
            readiness = evaluate_manifest_payload(payload, repo_root=root)
        except (OSError, TypeError, ValueError):
            unavailable += 1
            continue
        if readiness.is_ready:
            ready += 1
        else:
            unavailable += 1
    readiness = _check(
        "Model readiness",
        PASS if unavailable == 0 else WARN,
        f"{ready} enabled model(s) ready; {unavailable} not ready (no downloads attempted)",
    )
    return [symlinks, manifests, readiness]


def _optional_ai_tool_checks(run: CommandRunner) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for command in ("codex", "claude", "opencode", "ollama"):
        available = _result_problem(run((command, "--version"))) is None
        checks.append(
            _check(
                f"Optional AI tool: {command}",
                PASS,
                "available (informational)" if available else "not available (informational)",
            )
        )
    return checks


def overall_status(checks: Iterable[DoctorCheck]) -> str:
    statuses = {check.status for check in checks}
    return FAIL if FAIL in statuses else WARN if WARN in statuses else PASS


def exit_code_for_checks(checks: Iterable[DoctorCheck]) -> int:
    return {PASS: 0, WARN: 1, FAIL: 2}[overall_status(checks)]


def build_report(root: Path = ROOT, *, runner: CommandRunner | None = None) -> dict[str, Any]:
    """Collect a redacted report through bounded read-only diagnostics."""

    run = runner or (lambda argv: run_read_only(argv, cwd=root))
    checks = [
        *_git_checks(run),
        *_python_checks(root, run),
        *_node_checks(root, run),
        *_desktop_checks(root, run),
        *_environment_checks(root),
        *_model_checks(root),
        *_optional_ai_tool_checks(run),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": overall_status(checks),
        "exit_code": exit_code_for_checks(checks),
        "checks": [check.as_dict() for check in checks],
    }


def render_text(report: dict[str, Any]) -> str:
    lines = ["Creative AI Studio development environment doctor"]
    for check in report["checks"]:
        lines.append(f"[{check['status']}] {check['name']}: {check['summary']}")
        lines.extend(f"  - {detail}" for detail in check["details"])
    lines.append(f"Overall: {report['status']} (exit {report['exit_code']})")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect the development environment without changing it."
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON on stdout")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report()
    print(json.dumps(report, sort_keys=True) if args.json else render_text(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CommandResult",
    "DoctorCheck",
    "build_report",
    "exit_code_for_checks",
    "is_supported_node",
    "is_supported_python",
    "main",
    "overall_status",
    "parse_dotenv_keys",
    "parse_version",
    "parse_worktree_count",
    "render_text",
    "run_read_only",
]
