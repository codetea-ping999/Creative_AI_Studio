"""Run the issue-fleet workflow contract tests through the repository test gate."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NODE_TEST_PATH = REPOSITORY_ROOT / "tests" / "issue_fleet_workflow.test.mjs"


class IssueFleetWorkflowTests(unittest.TestCase):
    def test_workflow_contract(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to validate the agent harness")
        assert node is not None

        syntax_check = subprocess.run(
            [node, "--check", str(REPOSITORY_ROOT / ".claude" / "workflows" / "issue-fleet.js")],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(syntax_check.returncode, 0, syntax_check.stdout + syntax_check.stderr)

        result = subprocess.run(
            [node, "--test", str(NODE_TEST_PATH)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
