"""Contract tests for the generator execution context (#208).

These cover the acceptance criteria for a backend-neutral cancellation
handle that ``JobRunner`` builds and hands to generators, decoupled from
``JobService``/``JobRepository`` internals:

- a running generator can query whether cancellation has been requested
- existing generators without cancellation support continue to run unchanged
- context construction stays inside the job-execution layer (``JobRunner``)
- cancellation checks do not require direct ``JobRepository`` knowledge in
  media generators
- the runner/generator contract holds for fakes exercising every one of
  ``BaseGenerator.generate``'s supported signatures

``tests/test_job_pipeline.py`` already covers the same machinery as part of
broader job-pipeline scenarios; this file isolates the contract itself so it
stays discoverable and fast to read on its own.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from core.jobs import (
    CancellationRegistry,
    EventBus,
    GenerationCancelled,
    GenerationContext,
    JobQueue,
    JobRunner,
    JobService,
)
from core.schemas import GenerationRequest, GenerationResult
from core.storage.repositories.job_repository import JobRepository
from generators.base import BaseGenerator
from generators.registry import GeneratorRegistry


def _request(**overrides: object) -> GenerationRequest:
    payload: dict[str, object] = {
        "media_type": "audio",
        "prompt": "contract test",
        "model_id": "",
        "output_format": "wav",
        "params": {},
    }
    payload.update(overrides)
    return GenerationRequest(**payload)  # type: ignore[arg-type]


def _result(job_id: str, output_path: Path, **metadata: object) -> GenerationResult:
    return GenerationResult(
        job_id=job_id,
        status="succeeded",
        outputs=[str(output_path)],
        previews=[],
        metadata=dict(metadata),
        error_message=None,
    )


class _RigContext:
    """A repository, queue, service, and runner wired together for one test."""

    def __init__(self, tmp_dir: Path, generator: BaseGenerator, *, with_cancellation: bool) -> None:
        self.repository = JobRepository(tmp_dir / "jobs.db")
        self.queue = JobQueue()
        self.event_bus = EventBus()
        self.cancellation_registry = CancellationRegistry() if with_cancellation else None
        self.service = JobService(
            self.repository,
            self.queue,
            self.event_bus,
            cancellation_registry=self.cancellation_registry,
        )
        self.registry = GeneratorRegistry({"audio": generator})
        self.runner = JobRunner(
            self.repository,
            self.queue,
            self.registry,
            self.event_bus,
            job_service=self.service,
            cancellation_registry=self.cancellation_registry,
        )

    def submit(self) -> str:
        job = self.service.create_job(_request())
        return job.id


# ---------------------------------------------------------------------------
# Context construction stays inside the job-execution layer.
# ---------------------------------------------------------------------------


def test_runner_builds_context_regardless_of_cancellation_registry() -> None:
    """A generator never builds its own ``GenerationContext``; only the
    runner does. It always builds one now (#201 follow-up, eighth Codex
    round on PR #376) -- carrying project_id and progress reporting even
    without a ``CancellationRegistry`` backing it, since neither of those
    ever depended on cancellation support. Only ``is_cancelled`` itself
    is affected: with no registry to ever record a cancellation request
    in, it always reports "never cancelled" rather than the context being
    entirely absent (the prior legacy behavior)."""
    with TemporaryDirectory() as tmp_dir:
        repository = JobRepository(Path(tmp_dir) / "jobs.db")
        queue = JobQueue()
        registry = GeneratorRegistry({})

        without_registry = JobRunner(repository, queue, registry, cancellation_registry=None)
        context_without_registry = without_registry._begin_context(
            "job-a", project_id="project-a"
        )
        assert isinstance(context_without_registry, GenerationContext)
        assert context_without_registry.project_id == "project-a"
        assert context_without_registry.is_cancelled() is False

        with_registry = JobRunner(
            repository, queue, registry, cancellation_registry=CancellationRegistry()
        )
        context = with_registry._begin_context("job-b")
        assert isinstance(context, GenerationContext)


# ---------------------------------------------------------------------------
# A running generator can query cancellation, without any JobRepository
# knowledge of its own.
# ---------------------------------------------------------------------------


class _ContextOnlyGenerator(BaseGenerator):
    """Observes cancellation exclusively through ``context`` -- it never
    imports or references ``JobRepository``/``JobService``, demonstrating
    the contract holds without repository knowledge in the generator."""

    def __init__(self, output_dir: Path, *, steps: int, cancel_after: int, on_step) -> None:
        self.output_dir = output_dir
        self.steps = steps
        self.cancel_after = cancel_after
        self.on_step = on_step
        self.steps_completed = 0

    def validate_request(self, request: GenerationRequest) -> None:
        return None

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest, context=None):
        assert context is not None, "runner must supply a context when cancellation is wired"
        for step in range(1, self.steps + 1):
            if step == self.cancel_after:
                self.on_step()
            context.raise_if_cancelled()
            self.steps_completed = step
        output_path = self.output_dir / "output.wav"
        output_path.write_bytes(b"RIFFstub")
        return _result("context-only", output_path)

    def cleanup(self, request: GenerationRequest) -> None:
        return None


def test_generator_observes_cancellation_via_context_without_repository_access() -> None:
    with TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        job_holder: dict[str, str] = {}
        generator = _ContextOnlyGenerator(
            root / "outputs",
            steps=4,
            cancel_after=2,
            on_step=lambda: rig.service.cancel_job(job_holder["id"]),
        )
        # A generator satisfying this contract has no attribute referencing a
        # repository -- structurally proving cancellation checks do not
        # require direct JobRepository knowledge.
        assert not hasattr(generator, "job_repository")
        assert not hasattr(generator, "repository")

        rig = _RigContext(root, generator, with_cancellation=True)
        job_holder["id"] = rig.submit()

        final_job = rig.runner.run_once()

        assert final_job is not None
        assert final_job.status == "cancelled"
        # The generator stopped mid-loop, right after observing the request.
        assert generator.steps_completed == 1
        assert not (root / "outputs" / "output.wav").exists()


def test_generator_runs_to_completion_when_cancellation_is_never_requested() -> None:
    with TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        generator = _ContextOnlyGenerator(
            root / "outputs", steps=3, cancel_after=99, on_step=lambda: None
        )
        rig = _RigContext(root, generator, with_cancellation=True)
        rig.submit()

        final_job = rig.runner.run_once()

        assert final_job is not None
        assert final_job.status == "succeeded"
        assert generator.steps_completed == 3


# ---------------------------------------------------------------------------
# Existing generators without cancellation support continue to run
# unchanged, whether or not the surrounding infrastructure has a
# CancellationRegistry wired in.
# ---------------------------------------------------------------------------


class _NoContextParamGenerator(BaseGenerator):
    """Predates ``GenerationContext``: ``generate`` takes only ``request``."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.called = False

    def validate_request(self, request: GenerationRequest) -> None:
        return None

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest):  # type: ignore[override]
        self.called = True
        output_path = self.output_dir / "legacy.wav"
        output_path.write_bytes(b"RIFFlegacy")
        return _result("legacy", output_path, legacy=True)

    def cleanup(self, request: GenerationRequest) -> None:
        return None


@pytest.mark.parametrize("with_cancellation", [True, False])
def test_legacy_context_free_generator_is_unaffected_by_cancellation_wiring(
    with_cancellation: bool,
) -> None:
    with TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        generator = _NoContextParamGenerator(root / "outputs")
        rig = _RigContext(root, generator, with_cancellation=with_cancellation)
        rig.submit()

        final_job = rig.runner.run_once()

        assert final_job is not None
        assert final_job.status == "succeeded"
        assert generator.called is True
        assert (root / "outputs" / "legacy.wav").exists()


# ---------------------------------------------------------------------------
# BaseGenerator._generate_with_optional_context covers every generate()
# signature shape a generator may declare.
# ---------------------------------------------------------------------------


class _VarPositionalGenerator(BaseGenerator):
    """Declares ``*args`` instead of a named ``context`` parameter."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.received: object = "unset"

    def validate_request(self, request: GenerationRequest) -> None:
        return None

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest, *args):  # type: ignore[override]
        self.received = args[0] if args else None
        output_path = self.output_dir / "varargs.wav"
        output_path.write_bytes(b"RIFFstub")
        return _result("varargs", output_path)

    def cleanup(self, request: GenerationRequest) -> None:
        return None


class _VarKeywordGenerator(BaseGenerator):
    """Declares ``**kwargs`` instead of a named ``context`` parameter."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.received: object = "unset"

    def validate_request(self, request: GenerationRequest) -> None:
        return None

    def prepare(self, request: GenerationRequest) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest, **kwargs):  # type: ignore[override]
        self.received = kwargs.get("context", "missing")
        output_path = self.output_dir / "varkwargs.wav"
        output_path.write_bytes(b"RIFFstub")
        return _result("varkwargs", output_path)

    def cleanup(self, request: GenerationRequest) -> None:
        return None


def test_base_generator_passes_context_positionally_for_var_positional_signature() -> None:
    with TemporaryDirectory() as tmp_dir:
        generator = _VarPositionalGenerator(Path(tmp_dir))
        context = GenerationContext(is_cancelled=lambda: False)

        generator.run(_request(), context)

        assert generator.received is context


def test_base_generator_passes_context_as_keyword_for_var_keyword_signature() -> None:
    with TemporaryDirectory() as tmp_dir:
        generator = _VarKeywordGenerator(Path(tmp_dir))
        context = GenerationContext(is_cancelled=lambda: False)

        generator.run(_request(), context)

        assert generator.received is context


# ---------------------------------------------------------------------------
# The minimal integration surface: a GenerationContext backed by a
# CancellationRegistry, exercised directly without JobRunner in the loop.
# ---------------------------------------------------------------------------


def test_context_is_cancelled_reflects_registry_state_without_polling_a_repository() -> None:
    job_id = "job-direct"
    registry = CancellationRegistry()
    registry.begin(job_id)
    context = GenerationContext(is_cancelled=lambda: registry.is_cancelled(job_id))

    assert context.is_cancelled() is False
    context.raise_if_cancelled()  # does not raise yet

    registry.request_cancel(job_id)

    assert context.is_cancelled() is True
    with pytest.raises(GenerationCancelled):
        context.raise_if_cancelled()
