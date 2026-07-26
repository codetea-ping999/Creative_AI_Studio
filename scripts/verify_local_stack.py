#!/usr/bin/env python3
"""Run a single local verification flow for Creative AI Studio."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


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
        "--skip-web-tests",
        action="store_true",
        help="Skip the apps/web test suite.",
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
        default=None,
        help=(
            "Base URL to use for API smoke checks. With --start-api, the default "
            "uses an available loopback port."
        ),
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
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=(
            "Directory for temporary DB/output state. The default uses an "
            "automatically removed temporary directory."
        ),
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


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    timeout: float = 2.0,
) -> object:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local verification target
        response_body = response.read()
    if not response_body:
        return None
    return json.loads(response_body.decode("utf-8"))


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
    base = f"{base_url.rstrip('/')}/"
    health_url = urljoin(base, "health")
    models_url = urljoin(base, "models")

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

    project_id = _create_smoke_project(base)
    job_id = _create_smoke_video_job(base, project_id)
    _wait_for_smoke_job(base, job_id, timeout=timeout)
    _check_smoke_gallery(base, job_id)
    _check_smoke_project_jobs(base, project_id, job_id)


def _create_smoke_project(base_url: str) -> str:
    payload = request_json(
        urljoin(base_url, "projects"),
        method="POST",
        payload={
            "name": f"verify-smoke-{int(time.time())}",
            "description": "Created by scripts/verify_local_stack.py",
        },
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
        raise VerificationError(f"Unexpected /projects create payload: {payload!r}")
    project_id = payload["id"]
    print(f"[OK] API project create check: {project_id}")
    return project_id


def _create_smoke_video_job(base_url: str, project_id: str) -> str:
    payload = request_json(
        urljoin(base_url, "generate/video"),
        method="POST",
        payload={
            "prompt": "verification storyboard card",
            "model_id": "storyboard-video",
            "project_id": project_id,
            "output_format": "gif",
            "params": {
                "duration_seconds": 1,
                "fps": 4,
                "width": 256,
                "height": 144,
            },
        },
        timeout=5.0,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("job_id"), str):
        raise VerificationError(f"Unexpected /generate/video payload: {payload!r}")
    job_id = payload["job_id"]
    print(f"[OK] API video job create check: {job_id}")
    return job_id


def _wait_for_smoke_job(base_url: str, job_id: str, *, timeout: float) -> None:
    job_url = urljoin(base_url, f"jobs/{job_id}")
    deadline = time.time() + timeout
    last_payload: object = None
    while time.time() < deadline:
        last_payload = fetch_json(job_url)
        if not isinstance(last_payload, dict):
            raise VerificationError(f"Unexpected job payload: {last_payload!r}")
        status = last_payload.get("status")
        if status == "succeeded":
            print(f"[OK] API video job completed: {job_id}")
            return
        if status in {"failed", "cancelled"}:
            raise VerificationError(f"Smoke job ended with {status}: {last_payload!r}")
        time.sleep(0.5)
    raise VerificationError(f"Timed out waiting for smoke job {job_id}: {last_payload!r}")


def _check_smoke_gallery(base_url: str, job_id: str) -> None:
    payload = fetch_json(urljoin(base_url, "gallery?media_type=video"))
    if not isinstance(payload, list):
        raise VerificationError(f"Unexpected /gallery payload: {payload!r}")
    if not any(isinstance(item, dict) and item.get("job_id") == job_id for item in payload):
        raise VerificationError(f"Smoke job is missing from /gallery: {job_id}")
    print(f"[OK] API gallery check includes smoke job: {job_id}")


def _check_smoke_project_jobs(base_url: str, project_id: str, job_id: str) -> None:
    payload = fetch_json(urljoin(base_url, f"projects/{project_id}/jobs"))
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise VerificationError(f"Unexpected project jobs payload: {payload!r}")
    if not any(isinstance(item, dict) and item.get("id") == job_id for item in payload["jobs"]):
        raise VerificationError(f"Smoke job is missing from project jobs: {job_id}")
    print(f"[OK] API project jobs check includes smoke job: {job_id}")


def build_isolated_environment(
    runtime_root: Path,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    data_root = runtime_root / "data"
    output_root = runtime_root / "outputs"
    for path in (
        data_root,
        data_root / "projects",
        data_root / "feedback",
        output_root / "images",
        output_root / "audio",
        output_root / "videos",
    ):
        path.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ if base_env is None else base_env)
    for name in ("OUTPUT_IMAGE_DIR", "OUTPUT_AUDIO_DIR", "OUTPUT_VIDEO_DIR"):
        env.pop(name, None)
    env.update(
        {
            "DB_PATH": str(data_root / "jobs.db"),
            "OUTPUT_DIR": str(output_root),
        }
    )
    return env


def _available_loopback_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@contextmanager
def isolated_runtime_root(configured_root: Path | None):
    if configured_root is not None:
        configured_root.mkdir(parents=True, exist_ok=True)
        yield configured_root
        return
    with TemporaryDirectory(prefix="creative-ai-studio-verify-") as tmp_dir:
        yield Path(tmp_dir)


def build_api_command(
    base_url: str,
    *,
    base_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    env = dict(os.environ if base_env is None else base_env)
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
def managed_api_process(base_url: str, *, base_env: dict[str, str] | None = None):
    command, env = build_api_command(base_url, base_env=base_env)
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
        with isolated_runtime_root(args.runtime_root) as runtime_root:
            isolated_env = build_isolated_environment(runtime_root)
            api_base_url = args.api_base_url or (
                _available_loopback_url()
                if args.start_api
                else "http://127.0.0.1:8000"
            )

            if not args.skip_setup_check:
                command = [venv_python(), "scripts/check_local_setup.py"]
                if not args.check_runtime_files:
                    command.append("--skip-runtime-files")
                run_command(command, env=isolated_env)

            if not args.skip_web_tests:
                run_command(["npm", "test"], cwd=ROOT / "apps" / "web", env=isolated_env)

            if not args.skip_web_build:
                run_command(["npm", "run", "build"], cwd=ROOT / "apps" / "web", env=isolated_env)

            if not args.skip_tests:
                run_command([venv_python(), "-m", "pytest", "-q"], env=isolated_env)

            if not args.skip_api_smoke:
                if args.start_api:
                    with managed_api_process(api_base_url, base_env=isolated_env) as process:
                        try:
                            run_api_smoke_checks(api_base_url, timeout=args.api_timeout)
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
                    run_api_smoke_checks(api_base_url, timeout=args.api_timeout)

    except VerificationError as exc:
        print(f"[FAIL] {exc}")
        return 1

    print("[OK] Verification suite completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
