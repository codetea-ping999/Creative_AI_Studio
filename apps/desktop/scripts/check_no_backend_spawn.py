#!/usr/bin/env python3
"""Static smoke check for the Desktop Shell hard invariants.

The desktop shell is a UI-only resident wrapper. Its Rust core must never
spawn Python/FastAPI workers, initialize CUDA, or load AI model runtimes. This
script parses the desktop Rust source and fails if it finds code paths that
would violate that boundary, so the invariant is checkable without a Rust
toolchain (e.g. in CI or on a machine that only has Node/Python).

Exit code 0 = boundary respected. Non-zero (with a per-match report) otherwise.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]  # apps/desktop
SRC = ROOT / "src-tauri" / "src"

# Each probe is a (name, regex) that indicates a launch/runtime path the
# resident shell must not own.
PROBES = [
    ("process-command", re.compile(r"\bCommand\s*::\s*new\b")),
    ("std::process", re.compile(r"std\s*::\s*process")),
    ("spawn", re.compile(r"\.spawn\s*\(")),
    ("python", re.compile(r"\bpython(?:3(?:\.\d+)?)?\b", re.IGNORECASE)),
    ("uvicorn", re.compile(r"uvicorn", re.IGNORECASE)),
    ("fastapi", re.compile(r"fastapi", re.IGNORECASE)),
    ("cuda", re.compile(r"\bcuda\b", re.IGNORECASE)),
    ("torch", re.compile(r"\btorch\b", re.IGNORECASE)),
    ("model-load", re.compile(r"\bmodel(?:_|::|\.)?load", re.IGNORECASE)),
]

# Regex that approximates "code, not a comment/string", by replacing comment
# and string literals with spaces before probing. This keeps the check safe
# even though the Rust source legitimately *describes* the invariant in doc
# comments.
_COMMENT_STRIP = re.compile(
    r"""
    //[^\n]*                       # line comment
    | /\*.*?\*/                    # block comment
    | "(?:\\.|[^"\\])*"            # double-quoted string
    | b"(?:\\.|[^"\\])*"           # byte string
    | r#"[^"]*"#                   # raw string (common cases)
    """,
    re.DOTALL | re.VERBOSE,
)


def strip_comments_and_strings(text: str) -> str:
    return _COMMENT_STRIP.sub("", text)


def main() -> int:
    files = sorted(SRC.rglob("*.rs"))
    if not files:
        print(f"error: no Rust sources under {SRC}", file=sys.stderr)
        return 2

    hits: list[str] = []
    for file in files:
        raw_lines = file.read_text(encoding="utf-8").splitlines()
        text = "\n".join(raw_lines)
        code = strip_comments_and_strings(text)
        code_lines = code.splitlines()
        # Keep original line numbers for reporting.
        for name, pattern in PROBES:
            for line_no, line in enumerate(code_lines, start=1):
                if pattern.search(line):
                    hits.append(f"{file.relative_to(ROOT)}:{line_no} [{name}] {raw_lines[line_no - 1].strip()}")

    if hits:
        print("Desktop Shell hard-invariant violation(s) found:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        print(
            "\nThe desktop shell must remain a UI-only resident wrapper "
            "(docs/desktop/architecture-decision.md).",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(files)} Rust file(s) contain no backend/CUDA/model runtime spawn path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
