import importlib.util
import unittest
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[1] / "apps/desktop/scripts/check_no_backend_spawn.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_no_backend_spawn", _GUARD)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StripCommentsAndStringsTests(unittest.TestCase):
    def test_raw_strings_are_removed_without_touching_identifiers(self) -> None:
        guard = _load_guard()
        code = guard.strip_comments_and_strings(
            'let torch = 1; std::process::id(); let a = r#"python"#; '
            'let b = r"uvicorn"; let c = br##"fastapi"##; // cuda'
        )
        self.assertIn("torch", code)
        self.assertIn("std::process", code)
        for hidden in ("python", "uvicorn", "fastapi", "cuda"):
            self.assertNotIn(hidden, code)


if __name__ == "__main__":
    unittest.main()
