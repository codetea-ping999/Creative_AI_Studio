"""PR4b closure guard: production code must not reach around the lease API.

Issue #414's migration contract requires production generation to obtain a
runtime through `ModelService.acquire_runtime()` -- the leased, execution-
locked, admission-bounded path -- and explicitly requires the retained bare
`resolve_runtime()` / `get_runtime()` API to be audited and given a narrower
safe-use contract.

These are *static* AST guards rather than runtime monkeypatch assertions
(`tests/test_image_lease_cancellation.py`'s
`test_image_production_path_never_uses_bare_resolve_runtime` is the runtime
equivalent, and only covers the one code path that one test happens to
exercise). A static guard covers every production module unconditionally,
including paths no test drives yet, which is what makes it a regression
guard rather than a coverage artifact.

Deliberately *not* string matching: a `rg` for "resolve_runtime" matches the
method definition, every docstring that names it, this module's own comments,
and unrelated identifiers like `_resolve_runtime_dtype`. The AST walk below
counts only real call expressions and `getattr()` indirection.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Everything a shipped application actually executes. `tests/` is excluded on
# purpose: tests legitimately drive the legacy API to prove its own behavior
# (see tests/test_runtime_safety_core.py's
# `test_resolve_runtime_disposes_the_loaded_runtime_when_publication_is_rejected`
# and the cloud opt-in tests in tests/test_speech_audio.py).
PRODUCTION_DIRS = ("apps", "bootstrap", "core", "generators", "scripts")

BARE_RUNTIME_API = {"resolve_runtime", "get_runtime"}

# Minimal, reasoned allowlist. Each entry is (relative path, attribute name,
# reason). Anything not listed here is a failure.
BARE_API_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "core/models/service.py",
        "resolve_runtime",
    ): (
        "ModelService.get_runtime() is a thin wrapper that delegates to "
        "ModelService.resolve_runtime() and discards the manifest half of the "
        "result. This is the legacy API calling itself, not a production "
        "generation path reaching around the lease."
    ),
}

# Reaching past ModelService into the cache/loader directly would bypass
# admission and leasing just as effectively as the bare API does. Only
# `core/models/` itself may do this -- that *is* the implementation.
CACHE_LOADER_OWNER_PREFIX = "core/models/"


def _production_files() -> list[Path]:
    files: list[Path] = []
    for directory in PRODUCTION_DIRS:
        base = REPO_ROOT / directory
        if base.exists():
            files.extend(sorted(base.rglob("*.py")))
    assert files, "no production modules found -- guard would vacuously pass"
    return files


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _bare_api_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Real call/getattr uses of the bare runtime API, not definitions."""

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # obj.resolve_runtime(...) / obj.get_runtime(...)
            if isinstance(func, ast.Attribute) and func.attr in BARE_RUNTIME_API:
                found.append((func.attr, node.lineno))
            # getattr(obj, "resolve_runtime") -- indirection still counts
            elif (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in BARE_RUNTIME_API
            ):
                found.append((node.args[1].value, node.lineno))
    return found


def _cache_loader_bypasses(tree: ast.AST) -> list[tuple[str, int]]:
    """Direct runtime-cache / loader use, which bypasses admission entirely."""

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        try:
            receiver = ast.unparse(node.func.value)
        except Exception:  # pragma: no cover - defensive, unparse is total in 3.9+
            continue
        # `runtime_cache` is the ModelRuntimeCache; deliberately NOT a bare
        # `cache`, which also names unrelated caches (the semantic ScoreCache,
        # the assembly font cache) whose get/put have nothing to do with G.
        if attr in {"get", "put"} and receiver.endswith("runtime_cache"):
            found.append((f"{receiver}.{attr}", node.lineno))
        elif attr == "load" and receiver.endswith("loader"):
            found.append((f"{receiver}.{attr}", node.lineno))
    return found


def test_no_production_module_calls_the_bare_runtime_api() -> None:
    violations: list[str] = []
    for path in _production_files():
        rel = _relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for attr, line in _bare_api_calls(tree):
            if (rel, attr) in BARE_API_ALLOWLIST:
                continue
            violations.append(
                f"{rel}:{line} calls bare {attr}() -- production generation must "
                "use ModelService.acquire_runtime() (issue #414). If this call "
                "genuinely cannot execute, mutate, or retain the runtime, add it "
                "to BARE_API_ALLOWLIST with a written reason."
            )
    assert not violations, "\n".join(violations)


def test_no_production_module_reaches_past_model_service_into_cache_or_loader() -> None:
    violations: list[str] = []
    for path in _production_files():
        rel = _relative(path)
        if rel.startswith(CACHE_LOADER_OWNER_PREFIX):
            continue  # core/models/ is the implementation of this boundary
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for target, line in _cache_loader_bypasses(tree):
            violations.append(
                f"{rel}:{line} calls {target}() directly -- that bypasses "
                "admission (G), the per-model load lock and leasing just as "
                "the bare runtime API does. Go through "
                "ModelService.acquire_runtime()."
            )
    assert not violations, "\n".join(violations)


def test_every_generator_acquires_runtimes_only_through_a_context_manager() -> None:
    """A retained handle outside `with` re-opens the leak the lease closes.

    `acquire_runtime()` returns a `RuntimeHandle` that also supports manual
    `release()`, which is legitimate for the cache/service internals and for
    tests. Production generators must use the context-manager form so the
    lease is released on success, exception and cancellation alike (issue
    #414 invariant 11) without depending on a hand-written `finally`.
    """

    violations: list[str] = []
    generators_root = REPO_ROOT / "generators"
    for path in sorted(generators_root.rglob("*.py")):
        rel = _relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        # Lines where a call is the context expression of a `with`.
        context_managed = {
            item.context_expr.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            for item in node.items
            if isinstance(item.context_expr, ast.Call)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "acquire_runtime"
                and node.lineno not in context_managed
            ):
                # The one legitimate shape: a generator's own
                # `_acquire_runtime()` helper *returning* the handle for its
                # caller to use in a `with`. Anything else is a retained
                # handle.
                enclosing = [
                    fn
                    for fn in ast.walk(tree)
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.lineno <= node.lineno
                    and max(getattr(n, "lineno", fn.lineno) for n in ast.walk(fn))
                    >= node.lineno
                ]
                returned_from_helper = any(
                    fn.name == "_acquire_runtime"
                    and any(
                        isinstance(stmt, ast.Return)
                        and stmt.value is not None
                        and node.lineno
                        in {
                            getattr(inner, "lineno", -1) for inner in ast.walk(stmt)
                        }
                        for stmt in ast.walk(fn)
                    )
                    for fn in enclosing
                )
                if not returned_from_helper:
                    violations.append(
                        f"{rel}:{node.lineno} acquires a runtime outside a `with` "
                        "block; the lease must be context-managed so it releases "
                        "on success, exception and cancellation alike."
                    )
    assert not violations, "\n".join(violations)


def test_the_guard_itself_detects_a_planted_violation(tmp_path) -> None:
    """The guards above pass today; prove they are not vacuous."""

    planted = tmp_path / "planted.py"
    planted.write_text(
        "def run(service, cache, loader):\n"
        "    manifest, runtime = service.resolve_runtime('m', 'image')\n"
        "    other = service.get_runtime('m', 'image')\n"
        "    indirect = getattr(service, 'resolve_runtime')('m', 'image')\n"
        "    cached = self.runtime_cache.get('m')\n"
        "    loaded = loader.load(item)\n"
        "    return manifest, runtime, other, indirect, cached, loaded\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"), filename="planted.py")

    bare = _bare_api_calls(tree)
    assert sorted(name for name, _ in bare) == [
        "get_runtime",
        "resolve_runtime",
        "resolve_runtime",
    ]

    bypasses = _cache_loader_bypasses(tree)
    assert sorted(name for name, _ in bypasses) == [
        "loader.load",
        "self.runtime_cache.get",
    ]


def test_guard_ignores_definitions_docstrings_and_unrelated_same_named_methods() -> None:
    """No false positives on the shapes that legitimately mention the API."""

    source = (
        '"""A docstring naming resolve_runtime() and get_runtime()."""\n'
        "\n"
        "# A comment naming resolve_runtime() too.\n"
        "class ModelService:\n"
        "    def resolve_runtime(self, model_id, media_type, task_type=None):\n"
        '        """Definition, not a call."""\n'
        "        return None, None\n"
        "\n"
        "    def get_runtime(self, model_id, media_type, task_type=None):\n"
        "        return None\n"
        "\n"
        "class UnrelatedDtypeHelper:\n"
        "    def _resolve_runtime_dtype(self, requested, device, torch):\n"
        "        return requested\n"
        "\n"
        "def caller(helper):\n"
        "    return helper._resolve_runtime_dtype(1, 'cpu', None)\n"
    )
    tree = ast.parse(source, filename="unrelated.py")
    assert _bare_api_calls(tree) == []
    assert _cache_loader_bypasses(tree) == []
