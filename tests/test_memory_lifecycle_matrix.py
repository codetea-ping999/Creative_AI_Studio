"""Structural coverage for the memory-lifecycle experiment matrix (#351).

The matrix is a contract, not prose: #352 reports measurements keyed by its
scenario ids (`S1`..`S6`), boundary ids (`B0`..`B7`), and metric names, and
#353/#354/#356/#361 consume those same keys. A silent rename or a dropped row
would break that join long after the edit, so the identifiers and the
before/after pairs they exist for are asserted here.
"""

from __future__ import annotations

from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPOSITORY_ROOT / "docs" / "performance" / "memory-lifecycle-experiment-matrix.md"
DOCS_GUIDE_PATH = REPOSITORY_ROOT / "docs" / "README.md"

# (id, name) pairs. The name is part of the contract because results are
# reported as "S2 runtime_load", not as a bare number.
REQUIRED_SCENARIOS = (
    ("S1", "api_only"),
    ("S2", "runtime_load"),
    ("S3", "generation"),
    ("S4", "unload"),
    ("S5", "idle"),
    ("S6", "repeated_switch"),
)

REQUIRED_BOUNDARIES = (
    ("B0", "process_start"),
    ("B1", "imports_ready"),
    ("B2", "api_ready"),
    ("B3", "pre_load"),
    ("B4", "post_load"),
    ("B5", "post_generate"),
    ("B6", "post_unload"),
    ("B7", "post_exit"),
)

REQUIRED_METRICS = (
    "rss_kib",
    "peak_rss_bytes",
    "mps_current_allocated_bytes",
    "mps_driver_allocated_bytes",
    "sys_free_percent",
    "sys_pages_compressor",
    "load_duration_ms",
    "unload_duration_ms",
    "generate_duration_ms",
)

REQUIRED_METADATA_FIELDS = (
    "hardware_id",
    "os_version",
    "torch_version",
    "model_public_id",
    "model_revision",
    "seed",
    "cold_process",
    "warm_runtime",
)

# Derivations that keep control-plane cost separable from runtime cost, and
# that make the unload / process-exit boundaries comparable.
REQUIRED_DERIVATIONS = (
    "control_plane_overhead",
    "runtime_load_overhead",
    "unload_returned",
    "unload_residual",
    "exit_returned",
)


def _table_rows(text: str) -> list[list[str]]:
    """Return every markdown table row as a list of trimmed cells."""

    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue  # separator row
        rows.append(cells)
    return rows


def _row_starting_with(rows: list[list[str]], key: str) -> list[str] | None:
    wanted = f"`{key}`"
    for cells in rows:
        if cells and cells[0] == wanted:
            return cells
    return None


class MemoryLifecycleMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix_exists = MATRIX_PATH.is_file()
        cls.text = MATRIX_PATH.read_text(encoding="utf-8") if cls.matrix_exists else ""
        cls.rows = _table_rows(cls.text)

    def test_matrix_document_exists(self) -> None:
        self.assertTrue(
            self.matrix_exists,
            f"Missing experiment matrix document: {MATRIX_PATH}",
        )

    def test_docs_guide_links_the_matrix(self) -> None:
        guide = DOCS_GUIDE_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "./performance/memory-lifecycle-experiment-matrix.md",
            guide,
            "docs/README.md must link the memory-lifecycle experiment matrix.",
        )

    def test_every_scenario_is_defined_with_its_id_and_name(self) -> None:
        for scenario_id, name in REQUIRED_SCENARIOS:
            with self.subTest(scenario=scenario_id):
                row = _row_starting_with(self.rows, scenario_id)
                self.assertIsNotNone(
                    row, f"Scenario {scenario_id} has no row in the scenario matrix."
                )
                assert row is not None
                self.assertEqual(
                    f"`{name}`",
                    row[1],
                    f"Scenario {scenario_id} must keep the name {name!r}.",
                )
                self.assertIn(
                    f"### {scenario_id} `{name}`",
                    self.text,
                    f"Scenario {scenario_id} has no procedure section.",
                )

    def test_every_boundary_is_defined_with_an_observer(self) -> None:
        for boundary_id, name in REQUIRED_BOUNDARIES:
            with self.subTest(boundary=boundary_id):
                row = _row_starting_with(self.rows, boundary_id)
                self.assertIsNotNone(
                    row, f"Boundary {boundary_id} has no row in the boundary table."
                )
                assert row is not None
                self.assertEqual(
                    f"`{name}`",
                    row[1],
                    f"Boundary {boundary_id} must keep the name {name!r}.",
                )
                self.assertTrue(
                    row[3],
                    f"Boundary {boundary_id} must name who samples it.",
                )

    def test_unload_and_process_exit_are_before_after_comparisons(self) -> None:
        """The two boundaries #350's Gate 0 turns on must be paired, not single points."""

        self.assertIn(
            "`B5` → `B6`",
            self.text,
            "The unload boundary must be documented as a before/after pair.",
        )
        self.assertIn(
            "`B6` → `B7`",
            self.text,
            "The process-exit boundary must be documented as a before/after pair.",
        )
        b7_row = _row_starting_with(self.rows, "B7")
        self.assertIsNotNone(b7_row, "Boundary B7 has no row in the boundary table.")
        assert b7_row is not None
        self.assertIn(
            "external",
            b7_row[3],
            "`B7` is only observable from outside the exited process, so its "
            "observer cell must say so.",
        )

    def test_control_plane_and_runtime_overhead_are_separately_derivable(self) -> None:
        for derivation in REQUIRED_DERIVATIONS:
            with self.subTest(derivation=derivation):
                row = _row_starting_with(self.rows, derivation)
                self.assertIsNotNone(row, f"Derivation {derivation!r} has no formula row.")
                assert row is not None
                self.assertTrue(
                    row[1].startswith("`") and row[1].endswith("`"),
                    f"Derivation {derivation!r} must carry an explicit formula.",
                )

    def test_required_metrics_carry_a_unit_and_a_source(self) -> None:
        for metric in REQUIRED_METRICS:
            with self.subTest(metric=metric):
                row = _row_starting_with(self.rows, metric)
                self.assertIsNotNone(row, f"Metric {metric!r} is not defined.")
                assert row is not None
                self.assertTrue(row[1], f"Metric {metric!r} has no unit.")
                self.assertTrue(row[2], f"Metric {metric!r} has no acquisition method.")

    def test_required_metadata_fields_are_fixed(self) -> None:
        for field in REQUIRED_METADATA_FIELDS:
            with self.subTest(field=field):
                row = _row_starting_with(self.rows, field)
                self.assertIsNotNone(row, f"Metadata field {field!r} is not fixed by the matrix.")

    def test_shared_performance_contracts_are_referenced_not_redefined(self) -> None:
        """#295/#297 own the general metrics; this document may only point at them."""

        self.assertIn("#295", self.text)
        self.assertIn("#297", self.text)
        self.assertIn(
            "再定義しません",
            self.text,
            "The reuse boundary against #295 must be stated explicitly.",
        )

    def test_environment_limits_are_stated_rather_than_assumed(self) -> None:
        """A procedure that cannot run as written is not reproducible."""

        self.assertIn("M1 Max", self.text)
        for constraint in ("unload_model", "psutil", "MAX_CACHED_MODELS"):
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, self.text)


if __name__ == "__main__":
    unittest.main()
