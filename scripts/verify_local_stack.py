#!/usr/bin/env python3
"""Run a single local verification flow for Creative AI Studio."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


class VerificationError(RuntimeError):
    """Raised when the verification flow fails."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local verification suite used by developers and CI.",
    )
    parser.add_argument(
        "--skip-setup-check",
        action="store_true",
        help="Skip scripts/check_local_setup.py.",
    )
    parser.add_argument(
        "--skip-web-build",
        action="store_true",
        help="Skip the apps/web production build.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip pytest.",
    )
    parser.add_argument(
        "--skip-api-smoke",
        action="store_true",
        help="Skip /health and /models smoke checks.",
    )
    parser.add_argument(
        "--start-api",
        action="store_true",
        help="Start a temporary uvicorn process before running API smoke checks.",
    )
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8000",
        help="Base URL to use for API smoke checks.",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=20.0,
        help="Seconds to wait for the API to become ready.",
    )
    parser.add_argument(
        "--check-runtime-files",
        action="store_true",
        help="Require local runtime model files during setup validation.",
    )
    return parser.parse_args()


def venv_python() -> str:
    candidate = ROOT / "venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def run_command(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print(f"[RUN] {' '.join(command)}")
    completed = subprocess.run(command, cwd=cwd, env=env, check=False)
    if completed.returncode != 0:
        raise VerificationError(
            f"Command failed with exit code {completed.returncode}: {' '.join(command)}"
        )


def fetch_json(url: str) -> object:
    with urlopen(url, timeout=2.0) as response:  # noqa: S310 - local verification target
        return json.load(response)


def wait_for_json(url: str, *, timeout: float) -> object:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            return fetch_json(url)
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.5)
    raise VerificationError(f"Timed out waiting for {url}: {last_error}")


def run_api_smoke_checks(base_url: str, *, timeout: float) -> None:
    health_url = urljoin(f"{base_url.rstrip('/')}/", "health")
    models_url = urljoin(f"{base_url.rstrip('/')}/", "models")

    health_payload = wait_for_json(health_url, timeout=timeout)
    if health_payload != {"status": "ok"}:
        raise VerificationError(f"Unexpected /health payload: {health_payload!r}")
    print(f"[OK] API health check: {health_url}")

    models_payload = fetch_json(models_url)
    if not isinstance(models_payload, dict) or "models" not in models_payload:
        raise VerificationError(f"Unexpected /models payload: {models_payload!r}")
    if not isinstance(models_payload["models"], list):
        raise VerificationError(f"/models payload must include a list: {models_payload!r}")
    print(f"[OK] API models check: {models_url} ({len(models_payload['models'])} models)")


def build_api_command(base_url: str) -> tuple[list[str], dict[str, str]]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    env = os.environ.copy()
    env["API_HOST"] = host
    env["API_PORT"] = str(port)
    command = [
        venv_python(),
        "-m",
        "uvicorn",
        "apps.api.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    return command, env


@contextmanager
def managed_api_process(base_url: str):
    command, env = build_api_command(base_url)
    print(f"[RUN] {' '.join(command)}")
    process = subprocess.Popen(  # noqa: S603 - command is fully specified
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def read_process_output(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        return ""
    if process.poll() is None:
        return ""
    return process.stdout.read().strip()


def main() -> int:
    args = parse_args()

    try:
        if not args.skip_setup_check:
            command = [venv_python(), "scripts/check_local_setup.py"]
            if not args.check_runtime_files:
                command.append("--skip-runtime-files")
            run_command(command)

        if not args.skip_web_build:
            run_command(["npm", "run", "build"], cwd=ROOT / "apps" / "web")

        if not args.skip_tests:
            run_command([venv_python(), "-m", "pytest", "-q"])

        if not args.skip_api_smoke:
            if args.start_api:
                with managed_api_process(args.api_base_url) as process:
                    try:
                        run_api_smoke_checks(args.api_base_url, timeout=args.api_timeout)
                    except Exception as exc:
                        if process.poll() is None:
                            process.send_signal(signal.SIGTERM)
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                        output = read_process_output(process)
                        detail = f"{exc}\n{output}".strip()
                        raise VerificationError(detail) from exc
            else:
                run_api_smoke_checks(args.api_base_url, timeout=args.api_timeout)

    except VerificationError as exc:
        print(f"[FAIL] {exc}")
        return 1

    print("[OK] Verification suite completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
