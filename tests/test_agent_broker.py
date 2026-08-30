"""Contract tests for the provider-neutral agent broker.

All provider processes are local fake CLIs. These tests must never spend model
usage or depend on provider network availability.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BROKER = REPOSITORY_ROOT / "scripts" / "agent_broker.py"


class AgentBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-broker-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "agent-broker@example.invalid")
        self._git("config", "user.name", "Agent Broker Test")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-qm", "fixture")
        self.state_dir = self.root / "state"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("PROMPT_SECRET_SENTINEL inspect the fixture", encoding="utf-8")

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def _fake(self, name: str, body: str) -> Path:
        path = self.bin_dir / name
        path.write_text(
            f"#!{sys.executable}\n" + textwrap.dedent(body).lstrip(),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _task(
        self,
        *,
        workspace: Path | None = None,
        mode: str = "read-only",
        providers: list[str] | None = None,
        allow_fallback: bool = True,
        base_commit: str | None = None,
        timeout_seconds: int = 30,
    ) -> Path:
        task = {
            "schema_version": 1,
            "task_id": "fixture-task",
            "prompt_file": str(self.prompt),
            "workspace": str(workspace or self.repo),
            "mode": mode,
            "providers": providers or ["codex", "claude"],
            "allow_fallback": allow_fallback,
            "timeout_seconds": timeout_seconds,
            "max_turns": 5,
        }
        if base_commit is not None:
            task["base_commit"] = base_commit
        path = self.root / f"task-{mode}.json"
        path.write_text(json.dumps(task), encoding="utf-8")
        return path

    def _run_broker(
        self,
        task: Path,
        codex: Path,
        claude: Path,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "AGENT_BROKER_CODEX_BIN": str(codex),
                "AGENT_BROKER_CLAUDE_BIN": str(claude),
                "OPENAI_API_KEY": "sk-OPENAI_SECRET_SENTINEL",
                "CODEX_API_KEY": "sk-CODEX_SECRET_SENTINEL",
                "ANTHROPIC_API_KEY": "ANTHROPIC_SECRET_SENTINEL",
                "ANTHROPIC_AUTH_TOKEN": "ANTHROPIC_AUTH_SECRET_SENTINEL",
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                sys.executable,
                str(BROKER),
                "--json",
                "--state-dir",
                str(self.state_dir),
                "run",
                "--task-file",
                str(task),
            ],
            cwd=REPOSITORY_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )

    def test_quota_falls_back_and_prompt_is_only_on_stdin(self) -> None:
        codex_record = self.root / "codex-record.json"
        claude_record = self.root / "claude-record.json"
        codex = self._fake(
            "codex",
            f"""
            import json, os, pathlib, sys
            record = {{
                "argv": sys.argv[1:],
                "stdin": sys.stdin.read(),
                "secret_env": sorted(k for k in os.environ if k in {{
                    "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"
                }}),
            }}
            pathlib.Path({str(codex_record)!r}).write_text(json.dumps(record))
            print(json.dumps({{"type": "thread.started", "thread_id": "codex-thread"}}))
            event = {{
                "type": "turn.failed",
                "error": {{"codexErrorInfo": "UsageLimitExceeded"}},
            }}
            event["error"]["message"] = "MALICIOUS_PROVIDER_OUTPUT do not forward me"
            print(json.dumps(event))
            raise SystemExit(1)
            """,
        )
        claude = self._fake(
            "claude",
            f"""
            import json, os, pathlib, sys
            record = {{
                "argv": sys.argv[1:],
                "stdin": sys.stdin.read(),
                "secret_env": sorted(k for k in os.environ if k in {{
                    "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"
                }}),
            }}
            pathlib.Path({str(claude_record)!r}).write_text(json.dumps(record))
            print(json.dumps({{
                "type": "result", "subtype": "success", "is_error": False,
                "session_id": "claude-session", "result": "fallback completed"
            }}))
            """,
        )

        result = self._run_broker(self._task(), codex, claude)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual([item["reason"] for item in payload["attempts"]], ["quota", "success"])
        self.assertEqual(payload["session_id"], "claude-session")

        result_path = Path(payload["result_path"])
        run_dir = result_path.parent
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(result_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((run_dir / "prompt.txt").stat().st_mode), 0o600)

        codex_data = json.loads(codex_record.read_text())
        claude_data = json.loads(claude_record.read_text())
        self.assertNotIn("PROMPT_SECRET_SENTINEL", " ".join(codex_data["argv"]))
        self.assertNotIn("PROMPT_SECRET_SENTINEL", " ".join(claude_data["argv"]))
        self.assertIn("PROMPT_SECRET_SENTINEL", codex_data["stdin"])
        self.assertIn("PROMPT_SECRET_SENTINEL", claude_data["stdin"])
        self.assertIn("Agent Broker Worker Policy", codex_data["stdin"])
        self.assertIn("previous provider stopped", claude_data["stdin"])
        self.assertNotIn("MALICIOUS_PROVIDER_OUTPUT", claude_data["stdin"])
        self.assertEqual(codex_data["secret_env"], [])
        self.assertEqual(claude_data["secret_env"], [])

        codex_argv = codex_data["argv"]
        self.assertLess(codex_argv.index("--ask-for-approval"), codex_argv.index("exec"))
        self.assertGreater(codex_argv.index("--ignore-user-config"), codex_argv.index("exec"))
        self.assertNotIn("mcp_servers={}", codex_argv)
        self.assertIn("multi_agent", codex_argv)
        self.assertIn("hooks", codex_argv)
        self.assertIn("--safe-mode", claude_data["argv"])
        self.assertIn("--strict-mcp-config", claude_data["argv"])
        allowed = claude_data["argv"][claude_data["argv"].index("--allowedTools") + 1]
        workspace_glob = f"{self.repo.resolve()}/**"
        self.assertEqual(
            allowed,
            f"Read({workspace_glob}),Glob({workspace_glob}),Grep({workspace_glob})",
        )
        self.assertIn("Bash", next(arg for arg in claude_data["argv"] if "mcp__*" in arg))

        status = subprocess.run(
            [
                sys.executable,
                str(BROKER),
                "--json",
                "--state-dir",
                str(self.state_dir),
                "status",
                payload["run_id"],
                "--workspace",
                str(self.repo),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout), payload)

    def test_codex_success_requires_completed_event_and_last_message(self) -> None:
        codex = self._fake(
            "codex",
            """
            import json, pathlib, sys
            args = sys.argv[1:]
            sys.stdin.read()
            output = pathlib.Path(args[args.index("--output-last-message") + 1])
            output.write_text("codex completed", encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "codex-ok"}))
            print(json.dumps({"type": "turn.completed"}))
            """,
        )
        claude_marker = self.root / "claude-called"
        claude = self._fake(
            "claude",
            f"""
            import pathlib
            pathlib.Path({str(claude_marker)!r}).write_text("called")
            raise SystemExit(99)
            """,
        )

        result = self._run_broker(self._task(), codex, claude)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["output"], "codex completed")
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertFalse(claude_marker.exists())

    def test_claude_quota_falls_back_to_codex(self) -> None:
        claude = self._fake(
            "claude",
            """
            import json, sys
            sys.stdin.read()
            print(json.dumps({
                "type": "result", "subtype": "error_during_execution", "is_error": True,
                "session_id": "claude-limited", "result": "You've hit your usage limit"
            }))
            raise SystemExit(1)
            """,
        )
        codex = self._fake(
            "codex",
            """
            import json, pathlib, sys
            args = sys.argv[1:]
            sys.stdin.read()
            output = pathlib.Path(args[args.index("--output-last-message") + 1])
            output.write_text("codex fallback completed", encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "codex-fallback"}))
            print(json.dumps({"type": "turn.completed"}))
            """,
        )
        task = self._task(providers=["claude", "codex"])

        result = self._run_broker(task, codex, claude)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual([item["reason"] for item in payload["attempts"]], ["quota", "success"])
        self.assertEqual(payload["output"], "codex fallback completed")

    def test_protocol_error_does_not_fall_back(self) -> None:
        codex = self._fake(
            "codex",
            """
            import sys
            sys.stdin.read()
            print("not-json")
            """,
        )
        claude_marker = self.root / "claude-called"
        claude = self._fake(
            "claude",
            f"""
            import pathlib
            pathlib.Path({str(claude_marker)!r}).write_text("called")
            """,
        )

        result = self._run_broker(self._task(), codex, claude)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "protocol_error")
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertFalse(claude_marker.exists())

    def test_write_mode_rejects_main_checkout(self) -> None:
        marker = self.root / "provider-called"
        provider = self._fake(
            "provider",
            f"""
            import pathlib
            pathlib.Path({str(marker)!r}).write_text("called")
            """,
        )
        result = self._run_broker(
            self._task(mode="workspace-write", providers=["codex"]),
            provider,
            provider,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "write_requires_linked_worktree")
        self.assertFalse(marker.exists())

    def test_dirty_failed_write_blocks_fallback(self) -> None:
        worktree = self.root / "worker-worktree"
        self._git("worktree", "add", "-qb", "broker-worker", str(worktree))
        codex = self._fake(
            "codex",
            f"""
            import json, pathlib, sys
            sys.stdin.read()
            pathlib.Path({str(worktree / "partial.txt")!r}).write_text("partial")
            print(json.dumps({{"type": "thread.started", "thread_id": "dirty-codex"}}))
            event = {{
                "type": "turn.failed",
                "error": {{"codexErrorInfo": "UsageLimitExceeded"}},
            }}
            print(json.dumps(event))
            raise SystemExit(1)
            """,
        )
        claude_marker = self.root / "claude-called"
        claude = self._fake(
            "claude",
            f"""
            import pathlib
            pathlib.Path({str(claude_marker)!r}).write_text("called")
            """,
        )

        result = self._run_broker(
            self._task(workspace=worktree, mode="workspace-write"),
            codex,
            claude,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["reason"], "workspace_changed")
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertFalse(claude_marker.exists())
        self.assertTrue((worktree / "partial.txt").exists())

    def test_successful_worker_may_not_stage_changes(self) -> None:
        worktree = self.root / "staged-worktree"
        self._git("worktree", "add", "-qb", "broker-staged-worker", str(worktree))
        codex = self._fake(
            "codex",
            f"""
            import json, pathlib, subprocess, sys
            args = sys.argv[1:]
            sys.stdin.read()
            pathlib.Path({str(worktree / "staged.txt")!r}).write_text("staged")
            subprocess.run(["git", "add", "staged.txt"], cwd={str(worktree)!r}, check=True)
            output = pathlib.Path(args[args.index("--output-last-message") + 1])
            output.write_text("claimed success", encoding="utf-8")
            print(json.dumps({{"type": "thread.started", "thread_id": "staging-codex"}}))
            print(json.dumps({{"type": "turn.completed"}}))
            """,
        )
        claude = self._fake("claude", "raise SystemExit(99)\n")

        result = self._run_broker(
            self._task(workspace=worktree, mode="workspace-write"),
            codex,
            claude,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["reason"], "workspace_changed")
        self.assertEqual(len(payload["attempts"]), 1)

    def test_project_codex_mcp_is_explicitly_disabled(self) -> None:
        config = self.repo / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            '[mcp_servers.review_server]\ncommand = "untrusted"\nenabled = true\n',
            encoding="utf-8",
        )
        record_path = self.root / "codex-argv.json"
        codex = self._fake(
            "codex",
            f"""
            import json, pathlib, sys
            args = sys.argv[1:]
            sys.stdin.read()
            pathlib.Path({str(record_path)!r}).write_text(json.dumps(args))
            output = pathlib.Path(args[args.index("--output-last-message") + 1])
            output.write_text("done", encoding="utf-8")
            print(json.dumps({{"type": "thread.started", "thread_id": "codex-safe"}}))
            print(json.dumps({{"type": "turn.completed"}}))
            """,
        )
        claude = self._fake("claude", "raise SystemExit(99)\n")

        result = self._run_broker(
            self._task(providers=["codex"]), codex, claude
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = json.loads(record_path.read_text())
        self.assertIn("mcp_servers.review_server.enabled=false", argv)
        self.assertNotIn("mcp_servers={}", argv)
        self.assertIn("hooks", argv)

    def test_claude_write_permissions_are_scoped_and_include_write(self) -> None:
        worktree = self.root / "claude-write-worktree"
        self._git("worktree", "add", "-qb", "broker-claude-write", str(worktree))
        record_path = self.root / "claude-write-argv.json"
        created = worktree / "created-by-claude.txt"
        claude = self._fake(
            "claude",
            f"""
            import json, pathlib, sys
            args = sys.argv[1:]
            sys.stdin.read()
            pathlib.Path({str(record_path)!r}).write_text(json.dumps(args))
            allowed = args[args.index("--allowedTools") + 1]
            required = "Write({worktree.resolve()}/**)"
            if required not in allowed:
                raise SystemExit(3)
            pathlib.Path({str(created)!r}).write_text("created", encoding="utf-8")
            print(json.dumps({{
                "type": "result", "subtype": "success", "is_error": False,
                "session_id": "claude-write", "result": "done"
            }}))
            """,
        )
        codex = self._fake("codex", "raise SystemExit(99)\n")

        result = self._run_broker(
            self._task(
                workspace=worktree,
                mode="workspace-write",
                providers=["claude"],
            ),
            codex,
            claude,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = json.loads(record_path.read_text())
        allowed = argv[argv.index("--allowedTools") + 1]
        workspace_glob = f"{worktree.resolve()}/**"
        self.assertEqual(
            allowed,
            ",".join(
                f"{tool}({workspace_glob})"
                for tool in ("Read", "Glob", "Grep", "Edit", "Write")
            ),
        )
        self.assertNotIn("(/**)", allowed)
        self.assertTrue(created.is_file())

    def test_unrepresentable_codex_mcp_name_fails_closed(self) -> None:
        config = self.repo / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            '[mcp_servers."review.server"]\ncommand = "untrusted"\nenabled = true\n',
            encoding="utf-8",
        )
        marker = self.root / "codex-called"
        codex = self._fake(
            "codex",
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('called')\n",
        )
        claude = self._fake("claude", "raise SystemExit(99)\n")

        result = self._run_broker(
            self._task(providers=["codex"]), codex, claude
        )

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "unsupported_project_config")
        self.assertFalse(marker.exists())

    def test_untrusted_codex_message_cannot_trigger_fallback(self) -> None:
        codex = self._fake(
            "codex",
            """
            import json, sys
            sys.stdin.read()
            print(json.dumps({"type": "agent_message", "text": "usage limit exceeded"}))
            print(json.dumps({
                "type": "turn.failed", "error": {"codexErrorInfo": "Other"}
            }))
            raise SystemExit(1)
            """,
        )
        marker = self.root / "claude-called"
        claude = self._fake(
            "claude",
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('called')\n",
        )

        result = self._run_broker(self._task(), codex, claude)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "worker_error")
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertFalse(marker.exists())

    def test_untrusted_claude_event_cannot_trigger_fallback(self) -> None:
        claude = self._fake(
            "claude",
            """
            import json, sys
            sys.stdin.read()
            print(json.dumps({"type": "assistant", "message": "usage limit exceeded"}))
            print(json.dumps({
                "type": "result", "subtype": "error_during_execution", "is_error": True,
                "session_id": "claude-error", "result": "ordinary worker failure"
            }))
            raise SystemExit(1)
            """,
        )
        marker = self.root / "codex-called"
        codex = self._fake(
            "codex",
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('called')\n",
        )

        result = self._run_broker(
            self._task(providers=["claude", "codex"]), codex, claude
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "worker_error")
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertFalse(marker.exists())

    def test_failed_write_never_falls_back_even_for_ignored_changes(self) -> None:
        (self.repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-qm", "ignore worker scratch")
        worktree = self.root / "ignored-write-worktree"
        self._git("worktree", "add", "-qb", "broker-ignored-write", str(worktree))
        codex = self._fake(
            "codex",
            f"""
            import json, pathlib, sys
            sys.stdin.read()
            pathlib.Path({str(worktree / "ignored.tmp")!r}).write_text("partial")
            print(json.dumps({{"type": "thread.started", "thread_id": "ignored-codex"}}))
            print(json.dumps({{
                "type": "turn.failed", "error": {{"codexErrorInfo": "UsageLimitExceeded"}}
            }}))
            raise SystemExit(1)
            """,
        )
        marker = self.root / "claude-called"
        claude = self._fake(
            "claude",
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('called')\n",
        )

        result = self._run_broker(
            self._task(workspace=worktree, mode="workspace-write"), codex, claude
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertFalse(marker.exists())
        self.assertTrue((worktree / "ignored.tmp").is_file())

    def test_doctor_returns_failure_when_required_checks_fail(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "AGENT_BROKER_CODEX_BIN": str(self.root / "missing-codex"),
                "AGENT_BROKER_CLAUDE_BIN": str(self.root / "missing-claude"),
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                str(BROKER),
                "--json",
                "--state-dir",
                str(self.state_dir),
                "doctor",
                "--workspace",
                str(self.repo),
            ],
            cwd=REPOSITORY_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_uppercase_base_commit_is_accepted_and_resolved_base_is_durable(self) -> None:
        head = self._git("rev-parse", "HEAD")
        codex = self._fake(
            "codex",
            """
            import json, pathlib, sys
            args = sys.argv[1:]
            sys.stdin.read()
            output = pathlib.Path(args[args.index("--output-last-message") + 1])
            output.write_text("done", encoding="utf-8")
            print(json.dumps({"type": "thread.started", "thread_id": "codex-base"}))
            print(json.dumps({"type": "turn.completed"}))
            """,
        )
        claude = self._fake("claude", "raise SystemExit(99)\n")

        result = self._run_broker(
            self._task(providers=["codex"], base_commit=head.upper()), codex, claude
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["base_commit"], head)
        run_dir = Path(payload["result_path"]).parent
        stored_task = json.loads((run_dir / "task.json").read_text())
        self.assertEqual(stored_task["base_commit"], head)
        events = (run_dir / "events.jsonl").read_text()
        self.assertIn(head, events)

    def test_output_limit_terminates_worker_during_execution(self) -> None:
        marker = self.root / "overflow-finished"
        codex = self._fake(
            "codex",
            f"""
            import pathlib, sys, time
            sys.stdin.read()
            sys.stdout.buffer.write(b"x" * (9 * 1024 * 1024))
            sys.stdout.buffer.flush()
            time.sleep(30)
            pathlib.Path({str(marker)!r}).write_text("finished")
            """,
        )
        claude = self._fake("claude", "raise SystemExit(99)\n")
        started = time.monotonic()

        result = self._run_broker(
            self._task(providers=["codex"], timeout_seconds=10), codex, claude
        )

        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["reason"], "protocol_error")
        self.assertLess(elapsed, 8)
        self.assertFalse(marker.exists())
        stdout_path = Path(payload["attempts"][0]["stdout_path"])
        self.assertLessEqual(stdout_path.stat().st_size, 4 * 1024 * 1024)

    @unittest.skipIf(os.name == "nt", "process-group semantics are POSIX-only")
    def test_interrupt_terminates_worker_process_group(self) -> None:
        worker_pid_path = self.root / "worker.pid"
        codex = self._fake(
            "codex",
            f"""
            import os, pathlib, signal, sys, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            pathlib.Path({str(worker_pid_path)!r}).write_text(str(os.getpid()))
            sys.stdin.read()
            time.sleep(60)
            """,
        )
        claude = self._fake("claude", "raise SystemExit(99)\n")
        task = self._task(providers=["codex"])
        env = os.environ.copy()
        env.update(
            {
                "AGENT_BROKER_CODEX_BIN": str(codex),
                "AGENT_BROKER_CLAUDE_BIN": str(claude),
            }
        )
        broker = subprocess.Popen(
            [
                sys.executable,
                str(BROKER),
                "--json",
                "--state-dir",
                str(self.state_dir),
                "run",
                "--task-file",
                str(task),
            ],
            cwd=REPOSITORY_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        worker_pid: int | None = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not worker_pid_path.exists():
                time.sleep(0.02)
            self.assertTrue(worker_pid_path.exists(), "worker did not start")
            worker_pid = int(worker_pid_path.read_text())
            os.kill(broker.pid, signal.SIGINT)
            broker.communicate(timeout=12)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(worker_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("worker process survived broker interruption")
        finally:
            if broker.poll() is None:
                broker.kill()
                broker.communicate()
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
