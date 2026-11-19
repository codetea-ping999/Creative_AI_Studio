"""Deterministic unit coverage for the read-only development doctor."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import dev_doctor


class DevDoctorTests(unittest.TestCase):
    def test_parse_dotenv_keys_redacts_secrets_and_ports(self) -> None:
        keys = dev_doctor.parse_dotenv_keys(
            "API_HOST=127.0.0.1\nAPI_PORT=8000\n"
            "LOCAL_TEXT_ENDPOINT_API_KEY=not-for-output\nMODELS_ROOT=./models\n"
        )

        self.assertEqual(keys, frozenset({"API_HOST", "MODELS_ROOT"}))

    def test_supported_python_and_node_boundaries_follow_setup_contract(self) -> None:
        self.assertTrue(dev_doctor.is_supported_python((3, 10, 0)))
        self.assertFalse(dev_doctor.is_supported_python((3, 9, 99)))
        self.assertTrue(dev_doctor.is_supported_node((20, 19, 0)))
        self.assertFalse(dev_doctor.is_supported_node((20, 18, 99)))
        self.assertTrue(dev_doctor.is_supported_node((22, 12, 0)))
        self.assertFalse(dev_doctor.is_supported_node((22, 11, 99)))

    def test_status_classification_has_stable_exit_codes(self) -> None:
        passing = dev_doctor.DoctorCheck("check", dev_doctor.PASS, "ok")
        warning = dev_doctor.DoctorCheck("check", dev_doctor.WARN, "notice")
        failure = dev_doctor.DoctorCheck("check", dev_doctor.FAIL, "broken")

        self.assertEqual(dev_doctor.exit_code_for_checks([passing]), 0)
        self.assertEqual(dev_doctor.exit_code_for_checks([warning]), 1)
        self.assertEqual(dev_doctor.exit_code_for_checks([failure, warning]), 2)

    def test_run_read_only_uses_fixed_argv_timeout_and_no_shell(self) -> None:
        completed = subprocess.CompletedProcess(["git", "--version"], 0, "git version 2.45\n", "")
        with patch("scripts.dev_doctor.subprocess.run", return_value=completed) as run:
            result = dev_doctor.run_read_only(("git", "--version"), cwd=Path("/repo"))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_args.args[0], ["git", "--version"])
        self.assertEqual(run.call_args.kwargs["timeout"], dev_doctor.COMMAND_TIMEOUT_SECONDS)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_report_uses_read_only_commands_and_serialises_json(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepare_repo(root)
            commands: list[tuple[str, ...]] = []

            def runner(argv: tuple[str, ...]) -> dev_doctor.CommandResult:
                commands.append(tuple(argv))
                return self._successful_result(root, argv)

            with patch("scripts.dev_doctor._port_is_listening", return_value=False):
                report = dev_doctor.build_report(root, runner=runner)

        rendered = json.loads(json.dumps(report, sort_keys=True))
        self.assertEqual(rendered["status"], dev_doctor.PASS)
        self.assertEqual(rendered["exit_code"], 0)
        self.assertIn(("git", "rev-parse", "--short", "HEAD"), commands)
        self.assertIn(("git", "worktree", "prune", "--dry-run"), commands)
        self.assertIn((str(root / "venv" / "bin" / "python"), "-m", "pip", "check"), commands)
        self.assertIn(("rustc", "--version"), commands)
        self.assertIn(("cargo", "tauri", "--version"), commands)
        expected = {"name", "status", "summary", "details"}
        self.assertTrue(all(expected <= set(item) for item in rendered["checks"]))

    def test_failing_pip_check_is_a_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepare_repo(root)

            def runner(argv: tuple[str, ...]) -> dev_doctor.CommandResult:
                if tuple(argv[-3:]) == ("-m", "pip", "check"):
                    return dev_doctor.CommandResult(1, stderr="dependency conflict")
                return self._successful_result(root, argv)

            with patch("scripts.dev_doctor._port_is_listening", return_value=False):
                report = dev_doctor.build_report(root, runner=runner)

        dependency = next(
            item for item in report["checks"] if item["name"] == "Python dependency health"
        )
        self.assertEqual(dependency["status"], dev_doctor.FAIL)
        self.assertEqual(report["exit_code"], 2)
        self.assertNotIn("dependency conflict", json.dumps(report))

    def test_supported_venv_version_mismatch_warns(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepare_repo(root)
            alternate = (3, 10, 0) if sys.version_info[:2] != (3, 10) else (3, 11, 0)

            def runner(argv: tuple[str, ...]) -> dev_doctor.CommandResult:
                if tuple(argv) == (str(root / "venv" / "bin" / "python"), "--version"):
                    return dev_doctor.CommandResult(0, f"Python {alternate[0]}.{alternate[1]}.0\n")
                return self._successful_result(root, argv)

            checks = dev_doctor._python_checks(root, runner)

        alignment = next(check for check in checks if check.name == "Python interpreter alignment")
        self.assertEqual(alignment.status, dev_doctor.WARN)
        self.assertIn("major/minor differs", alignment.summary)

    def test_environment_missing_names_are_safe_and_actionable(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env.example").write_text(
                "API_HOST=127.0.0.1\nAPI_PORT=8000\n"
                "SAFE_MISSING=value\nEXAMPLE_TOKEN=secret-value\n",
                encoding="utf-8",
            )
            (root / ".env").write_text("API_HOST=127.0.0.1\n", encoding="utf-8")

            checks = dev_doctor._environment_checks(root)

        environment = next(check for check in checks if check.name == "Environment template")
        details = "\n".join(environment.details)
        self.assertEqual(environment.status, dev_doctor.WARN)
        self.assertIn("add missing safe keys to .env: SAFE_MISSING", details)
        self.assertNotIn("API_PORT", details)
        self.assertNotIn("EXAMPLE_TOKEN", details)
        self.assertNotIn("secret-value", details)

    def test_broken_model_symlink_is_a_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepare_repo(root)
            (root / "models" / "broken-link").symlink_to(root / "models" / "missing")

            checks = dev_doctor._model_checks(root)

        symlinks = next(check for check in checks if check.name == "Model symlinks")
        self.assertEqual(symlinks.status, dev_doctor.FAIL)

    def test_invalid_arguments_use_exit_code_two(self) -> None:
        with io.StringIO() as stderr, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as context:
                dev_doctor.parse_args(["--unsupported"])

        self.assertEqual(context.exception.code, 2)

    @staticmethod
    def _prepare_repo(root: Path) -> None:
        (root / "venv" / "bin").mkdir(parents=True)
        (root / "venv" / "bin" / "python").touch()
        (root / "apps" / "web" / "node_modules").mkdir(parents=True)
        cargo = root / "apps" / "desktop" / "src-tauri"
        cargo.mkdir(parents=True)
        (cargo / "Cargo.toml").write_text('[package]\nrust-version = "1.88"\n', encoding="utf-8")
        (root / "models" / "template").mkdir(parents=True)
        manifests = root / "models" / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "template.json").write_text(
            '{"id":"template","runtime":"template","local_path":"models/template"}',
            encoding="utf-8",
        )
        environment = "API_HOST=127.0.0.1\nAPI_PORT=8000\nWEB_PORT=5173\nMODELS_ROOT=./models\n"
        (root / ".env.example").write_text(environment, encoding="utf-8")
        (root / ".env").write_text(environment, encoding="utf-8")

    @staticmethod
    def _successful_result(root: Path, argv: tuple[str, ...]) -> dev_doctor.CommandResult:
        responses = {
            ("git", "--version"): "git version 2.45.0\n",
            ("git", "branch", "--show-current"): "feature/doctor\n",
            ("git", "rev-parse", "--short", "HEAD"): "a1b2c3d\n",
            ("git", "status", "--porcelain=v1"): "",
            ("git", "worktree", "list", "--porcelain"): "worktree /repo\nHEAD abc\n",
            ("git", "worktree", "prune", "--dry-run"): "",
            (str(root / "venv" / "bin" / "python"), "--version"):
                f"Python {sys.version_info.major}.{sys.version_info.minor}.0\n",
            (str(root / "venv" / "bin" / "python"), "-m", "pip", "--version"): "pip 24.0\n",
            (str(root / "venv" / "bin" / "python"), "-m", "pip", "check"):
                "No broken requirements found.\n",
            (
                str(root / "venv" / "bin" / "python"),
                "-B",
                "-c",
                "import fastapi, pydantic; print('runtime-import-ok')",
            ): "runtime-import-ok\n",
            ("node", "--version"): "v20.19.0\n",
            ("npm", "--version"): "10.8.0\n",
            ("npm", "--prefix", str(root / "apps" / "web"), "ls", "--depth=0", "--json"): "{}",
            ("cargo", "--version"): "cargo 1.88.0\n",
            ("rustc", "--version"): "rustc 1.88.0\n",
            ("cargo", "tauri", "--version"): "tauri-cli 2.0.0\n",
        }
        command = tuple(argv)
        if command in responses:
            return dev_doctor.CommandResult(0, responses[command])
        if command[0] in {"codex", "claude", "opencode", "ollama"}:
            return dev_doctor.CommandResult(None, unavailable=True)
        raise AssertionError(f"unexpected command: {command!r}")


if __name__ == "__main__":
    unittest.main()
