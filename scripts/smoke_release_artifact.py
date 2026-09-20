#!/usr/bin/env python3
"""
smoke_release_artifact.py

Full smoke test for the Creative AI Studio release artifact:
- unpack to fresh dir
- create venv
- pip install requirements
- run server on non-default loopback port without Node
- verify /, assets, /health, /models
- Stable HTTP journey (Project -> Template Story -> Procedural Visual -> Gallery -> Assembly MP4)
- MP4 generation verification
- SIGTERM clean shutdown
- reliability proofs (cancellation, startup recovery, persistence upgrade, data ownership)
"""

from __future__ import annotations

import os
import sys
import time
import subprocess
import tarfile
import tempfile
import signal
import re
from pathlib import Path

ARTIFACT_VERSION = "v1.0.0"
ARTIFACT_NAME = f"creative-ai-studio-{ARTIFACT_VERSION}.tar.gz"
SCENE_COUNT = 2
API_PORT = 8123
BASE_URL = f"http://127.0.0.1:{API_PORT}"
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    msg = f"{status}: {name}"
    if detail:
        msg += f" | {detail}"
    print(msg)
    if not cond:
        FAILURES.append(name)


def run_cmd(cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"CMD FAILED: {' '.join(cmd)}")
        print(f"stdout: {result.stdout[-500:]}")
        print(f"stderr: {result.stderr[-500:]}")
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def extract_asset_paths(index_html: str) -> tuple[str | None, str | None]:
    """Extract JS and CSS asset paths from index.html."""
    js_match = re.search(r'<script type="module"[^>]*src="(/assets/index-[^"]+\.js)"', index_html)
    css_match = re.search(r'<link rel="stylesheet"[^>]*href="(/assets/index-[^"]+\.css)"', index_html)
    return js_match.group(1) if js_match else None, css_match.group(1) if css_match else None


def wait_for_server(base_url: str, timeout: float = 60.0) -> bool:
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(f"{base_url}/health")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    print(f"  Server ready after {attempt} attempts")
                    return True
                else:
                    print(f"  Attempt {attempt}: /health returned {resp.status}")
        except Exception as e:
            if attempt % 10 == 0:
                print(f"  Attempt {attempt}: {type(e).__name__}: {e}")
        time.sleep(0.5)
    return False


def http_get(url: str, timeout: float = 30.0) -> tuple[int, bytes, dict]:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            headers = dict(resp.headers)
            return resp.status, body, headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def http_post(url: str, json_data: dict, timeout: float = 30.0) -> tuple[int, bytes, dict]:
    import urllib.request
    import urllib.error
    import json
    data = json.dumps(json_data).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            headers = dict(resp.headers)
            return resp.status, body, headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def verify_ui(artifact_root: Path) -> None:
    """Verify /, /health, assets, /models endpoints."""
    import json
    # /health
    status, body, _ = http_get(f"{BASE_URL}/health")
    check("/health 200", status == 200, body[:100])
    check("/health json ok", json.loads(body).get("status") == "ok")

    # /
    status, body, _ = http_get(BASE_URL)
    check("/ serves index.html", status == 200)
    root_html = body.decode('utf-8')
    check("/ index.html has doctype", "<!doctype html>" in root_html.lower())

    # Extract asset paths from served index.html
    js_path, css_path = extract_asset_paths(root_html)
    check("index.html has JS asset", js_path is not None)
    check("index.html has CSS asset", css_path is not None)

    # asset - use dynamic paths
    if js_path:
        status, _, _ = http_get(f"{BASE_URL}{js_path}")
        check("/assets JS 200", status == 200, js_path)

    if css_path:
        status, _, _ = http_get(f"{BASE_URL}{css_path}")
        check("/assets CSS 200", status == 200, css_path)

    # /models - strict: HTTP 200, valid JSON dict with a "models" list
    models_status, models_body, _ = http_get(f"{BASE_URL}/models")
    check("/models status 200", models_status == 200, f"status={models_status}")
    if models_status == 200:
        try:
            models_payload = json.loads(models_body)
        except json.JSONDecodeError:
            models_payload = None
        check(
            "/models payload is dict with models list",
            isinstance(models_payload, dict) and isinstance(models_payload.get("models"), list),
        )

    # byte-identical to dist
    dist_index = artifact_root / "apps/web/dist/index.html"
    served_bytes = body
    dist_bytes = dist_index.read_bytes()
    check("served index == dist index", served_bytes == dist_bytes)


def poll_job(job_id: str, timeout: float = 180.0) -> dict:
    import json
    import time
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, body, _ = http_get(f"{BASE_URL}/jobs/{job_id}")
        if status == 200:
            last = json.loads(body)
            if last.get("status") in ("succeeded", "failed", "cancelled"):
                if last.get("status") == "failed":
                    print(f"  Job {job_id} failed: {last.get('error_message')}")
                return last
        time.sleep(0.5)
    return last


def run_stable_journey(artifact_root: Path) -> tuple[str, str]:
    """Run the Stable creative journey and return (project_id, mp4_path)."""
    import json
    import urllib.parse

    # 1. Project
    status, body, _ = http_post(f"{BASE_URL}/projects", {"name": "Stable film"})
    check("POST /projects 201", status == 201)
    project_id = json.loads(body)["id"]
    check("project_id present", bool(project_id))

    # 2. Story (template: scene_list via template-writer)
    status, body, _ = http_post(
        f"{BASE_URL}/stories",
        {
            "title": "Rewind",
            "premise": "時を巻き戻せる少女が最後の一日を選び直す",
            "language": "ja",
            "project_id": project_id,
        },
    )
    check("POST /stories 201", status == 201)
    story_id = json.loads(body)["id"]

    # 3. Expand (template-writer)
    status, body, _ = http_post(
        f"{BASE_URL}/stories/{story_id}/expand",
        {"task": "scene_list", "model_id": "template-writer", "params": {"scene_count": SCENE_COUNT}},
    )
    check("POST /stories/{id}/expand 201", status == 201)
    expand_job_id = json.loads(body)["job_id"]
    j = poll_job(expand_job_id)
    check("expand job succeeded", j.get("status") == "succeeded")

    # 4. Apply
    status, body, _ = http_post(f"{BASE_URL}/stories/{story_id}/apply", {"job_id": expand_job_id})
    check("POST /stories/{id}/apply 200", status == 200)
    scenes = json.loads(body).get("story", {}).get("scenes", [])
    check(f"apply binds {SCENE_COUNT} scenes", len(scenes) == SCENE_COUNT)
    scene_ids = [s["id"] for s in scenes]

    # 5. Procedural visual per scene (storyboard-video)
    visual_jobs = {}
    for sid in scene_ids:
        status, body, _ = http_post(
            f"{BASE_URL}/stories/{story_id}/scenes/{sid}/generate",
            {"role": "visual", "media_type": "video"},
        )
        check(f"POST /scenes/{sid}/generate 201", status == 201)
        jid = json.loads(body)["job_id"]
        visual_jobs[sid] = jid
        j = poll_job(jid)
        check(f"procedural visual {sid} succeeded", j.get("status") == "succeeded", str(j.get("error_message")))

    # 6. Verify assets bound
    status, body, _ = http_get(f"{BASE_URL}/stories/{story_id}")
    detail = json.loads(body)
    missing = [e for e in detail.get("missing_assets", []) if e.get("role") == "visual"]
    check("no missing visual assets", missing == [])

    # 7. Gallery shows procedural clips
    gallery_url = f"{BASE_URL}/gallery?{urllib.parse.urlencode({'project_id': project_id, 'media_type': 'video'})}"
    status, body, _ = http_get(gallery_url)
    gallery = json.loads(body)
    gallery_by_job = {it["job_id"]: it for it in gallery}
    for s in detail["story"]["scenes"]:
        it = gallery_by_job.get(visual_jobs[s["id"]])
        check(f"gallery shows scene {s['id'][:8]}", it is not None and it["output_path"].endswith(".gif"))

    # 8. Assembly MP4
    status, body, _ = http_post(
        f"{BASE_URL}/stories/{story_id}/assemble",
        {"width": 320, "height": 180, "fps": 8},
    )
    check("POST /stories/{id}/assemble 201", status == 201)
    asm_job_id = json.loads(body)["job_id"]
    j = poll_job(asm_job_id)
    check("assembly job succeeded", j.get("status") == "succeeded")

    # 9. Gallery shows exactly one MP4
    status, body, _ = http_get(gallery_url)
    gallery2 = json.loads(body)
    mp4_items = [it for it in gallery2 if it["job_id"] == asm_job_id]
    check("gallery lists exactly one MP4", len(mp4_items) == 1)
    output_path = mp4_items[0]["output_path"]
    check("mp4 output_path suffix .mp4", output_path.endswith(".mp4"))

    # 10. Verify MP4 on disk
    p = artifact_root / output_path
    check("mp4 exists on disk >0", p.is_file() and p.stat().st_size > 0, str(p))
    data = p.read_bytes()
    check("mp4 has ftyp box", b"ftyp" in data[:32], data[:16].hex())

    # 11. Probe with bundled ffmpeg
    ffmpeg_bin = list((artifact_root / "venv/lib").glob("python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*"))
    if ffmpeg_bin:
        result = subprocess.run([str(ffmpeg_bin[0]), "-i", str(p), "-hide_banner"], capture_output=True, text=True, timeout=30)
        check("ffmpeg probe shows Video:", "Video:" in result.stderr, result.stderr[:200])

    return project_id, output_path


def run_reliability_proofs(artifact_root: Path, venv_python: Path) -> None:
    """Run existing reliability proof tests via pytest."""
    tests = [
        "tests/test_startup_recovery.py",
        "tests/test_data_directory_ownership.py",
        "tests/test_persistence_upgrade.py",
        "tests/test_stable_creative_journey.py",
    ]
    # Run core proofs
    result = run_cmd(
        [str(venv_python), "-m", "pytest", "-q"] + tests,
        cwd=artifact_root,
        timeout=600,
    )
    check("reliability proofs (startup recovery / data ownership / persistence upgrade / journey)", result.returncode == 0, result.stdout[-300:])

    # Cancellation subset
    result = run_cmd(
        [str(venv_python), "-m", "pytest", "-q", "tests/test_job_pipeline.py", "-k", "cancel"],
        cwd=artifact_root,
        timeout=300,
    )
    check("cancellation proofs (test_job_pipeline -k cancel)", result.returncode == 0, result.stdout[-300:])


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    artifact_path = repo_root / "artifacts" / ARTIFACT_NAME

    if not artifact_path.exists():
        print(f"ERROR: artifact not found at {artifact_path}")
        print("Run scripts/build_release_artifact.sh first, or copy artifact to artifacts/")
        return 1

    print(f"=== Smoke test for {ARTIFACT_NAME} ===")
    print(f"Repo root: {repo_root}")
    print(f"Artifact: {artifact_path}")

    with tempfile.TemporaryDirectory(prefix="smoke-") as tmpdir:
        tmp = Path(tmpdir)
        unpack_dir = tmp / "unpack"
        unpack_dir.mkdir()

        print("=== Unpacking artifact ===")
        with tarfile.open(artifact_path, "r:gz") as tf:
            tf.extractall(unpack_dir)

        # Artifact extracts to creative-ai-studio-v1.0.0/
        artifact_root = unpack_dir / f"creative-ai-studio-{ARTIFACT_VERSION}"
        if not artifact_root.exists():
            print(f"ERROR: expected {artifact_root} after extraction")
            return 1

        print("=== Creating fresh venv ===")
        venv_dir = artifact_root / "venv"
        run_cmd([sys.executable, "-m", "venv", str(venv_dir)], cwd=artifact_root)
        venv_python = venv_dir / "bin" / "python"
        venv_pip = venv_dir / "bin" / "pip"

        print("=== Upgrading pip ===")
        run_cmd([str(venv_pip), "install", "--upgrade", "pip", "-q"], cwd=artifact_root, timeout=120)

        print("=== Installing requirements ===")
        run_cmd([str(venv_pip), "install", "-r", "requirements.txt", "--disable-pip-version-check", "-q"], cwd=artifact_root, timeout=2400)

        # Verify imports
        run_cmd([str(venv_python), "-c", "import torch, imageio_ffmpeg, uvicorn, fastapi, httpx; print('imports ok')"], cwd=artifact_root)

        print("=== Starting server on port {API_PORT} (Node-free) ===")
        # Match manual test: minimal PATH + locale for UTF-8
        clean_path = "/bin:/usr/bin:/usr/sbin:/sbin"
        env = {"PATH": clean_path, "API_PORT": str(API_PORT), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        log_file = artifact_root / "smoke-server.log"
        log_fh = open(log_file, "w")
        try:
            server_proc = subprocess.Popen(
                ["bash", "scripts/run_studio.sh"],
                cwd=artifact_root,
                env={**os.environ, **env},
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            # Give server a moment to start before health checks (stdout buffering)
            time.sleep(2)

            try:
                print("=== Waiting for server ===")
                if not wait_for_server(BASE_URL):
                    print("Server did not start in time")
                    log_fh.flush()
                    with open(log_file) as f:
                        print(f.read()[-2000:])
                    return 1

                print("=== Verifying UI endpoints ===")
                verify_ui(artifact_root)

                print("=== Running Stable Journey ===")
                try:
                    run_stable_journey(artifact_root)
                except Exception as e:
                    print(f"ERROR during journey: {e}")
                    # Print server log for debugging
                    log_file = artifact_root / "smoke-server.log"
                    if log_file.exists():
                        print("=== Server log ===")
                        print(log_file.read_text()[-5000:])
                    raise

                print("=== Running reliability proofs ===")
                run_reliability_proofs(artifact_root, venv_python)

            finally:
                print("=== Shutting down server (SIGTERM) ===")
                server_proc.send_signal(signal.SIGTERM)
                try:
                    server_proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server_proc.kill()
                    server_proc.wait(timeout=5)

                # Verify port released
                time.sleep(1)
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex(("127.0.0.1", API_PORT))
                sock.close()
                check("port released after SIGTERM", result != 0)
        finally:
            log_fh.close()

    if FAILURES:
        print(f"\n=== SMOKE TEST FAILED: {len(FAILURES)} failure(s) ===")
        for f in FAILURES:
            print(f"  - {f}")
        return 1

    print("\n=== ALL SMOKE TESTS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())