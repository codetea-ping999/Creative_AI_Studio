"""Tests for the job status-transition contract (core/jobs/statuses.py).

These cover the acceptance criteria for the ``cancel_requested`` status
contract (#206): it is non-terminal, only resolves to documented terminal
states, queued cancellation stays distinguishable from running cancellation,
terminal jobs never move back into an active status, and repeated
cancellation requests are idempotent rather than invalid.
"""

from __future__ import annotations

import pytest

from core.jobs.statuses import (
    ACTIVE_JOB_STATUSES,
    ALLOWED_TRANSITIONS,
    JOB_STATUS_CANCEL_REQUESTED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_POSTPROCESSING,
    JOB_STATUS_PREPARING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    is_terminal_status,
    is_valid_transition,
)

RUNNING_LIKE_STATUSES = (
    JOB_STATUS_PREPARING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_POSTPROCESSING,
)


def test_status_partition_is_exhaustive_and_disjoint() -> None:
    """Every status is either active or terminal, never both, never neither."""

    assert set(JOB_STATUSES) == set(ACTIVE_JOB_STATUSES) | set(TERMINAL_JOB_STATUSES)
    assert set(ACTIVE_JOB_STATUSES).isdisjoint(TERMINAL_JOB_STATUSES)
    assert len(JOB_STATUSES) == len(set(JOB_STATUSES))


def test_allowed_transitions_only_reference_known_statuses() -> None:
    """Guards against a typo silently creating an unreachable/dead status."""

    assert set(ALLOWED_TRANSITIONS.keys()) == set(JOB_STATUSES)
    for targets in ALLOWED_TRANSITIONS.values():
        assert set(targets) <= set(JOB_STATUSES)


class TestCancelRequestedIsNonTerminal:
    def test_cancel_requested_is_not_a_terminal_status(self) -> None:
        assert JOB_STATUS_CANCEL_REQUESTED not in TERMINAL_JOB_STATUSES
        assert is_terminal_status(JOB_STATUS_CANCEL_REQUESTED) is False

    def test_cancel_requested_is_an_active_status(self) -> None:
        assert JOB_STATUS_CANCEL_REQUESTED in ACTIVE_JOB_STATUSES

    @pytest.mark.parametrize("target", [JOB_STATUS_CANCELLED, JOB_STATUS_FAILED])
    def test_cancel_requested_can_reach_its_documented_terminal_states(
        self, target: str
    ) -> None:
        assert is_valid_transition(JOB_STATUS_CANCEL_REQUESTED, target) is True

    def test_cancel_requested_can_never_resolve_to_succeeded(self) -> None:
        # A generation that races a cancel request must not be reported as a
        # success: cancel always wins.
        assert is_valid_transition(JOB_STATUS_CANCEL_REQUESTED, JOB_STATUS_SUCCEEDED) is False

    @pytest.mark.parametrize(
        "target",
        [JOB_STATUS_QUEUED, JOB_STATUS_PREPARING, JOB_STATUS_RUNNING, JOB_STATUS_POSTPROCESSING],
    )
    def test_cancel_requested_cannot_move_back_into_an_active_status(self, target: str) -> None:
        assert is_valid_transition(JOB_STATUS_CANCEL_REQUESTED, target) is False


class TestQueuedCancellationIsDistinguishableFromRunningCancellation:
    def test_queued_cancels_immediately_to_the_terminal_state(self) -> None:
        assert is_valid_transition(JOB_STATUS_QUEUED, JOB_STATUS_CANCELLED) is True

    def test_queued_never_passes_through_cancel_requested(self) -> None:
        # A queued job has nothing running to interrupt, so it must not be
        # able to land in the "waiting for cooperative shutdown" state.
        assert is_valid_transition(JOB_STATUS_QUEUED, JOB_STATUS_CANCEL_REQUESTED) is False

    @pytest.mark.parametrize("running_like", RUNNING_LIKE_STATUSES)
    def test_running_like_statuses_request_cancellation_instead_of_cancelling_directly(
        self, running_like: str
    ) -> None:
        assert is_valid_transition(running_like, JOB_STATUS_CANCEL_REQUESTED) is True
        # In-flight work cannot be discarded synchronously; it must go
        # through cancel_requested rather than jumping straight to cancelled.
        assert is_valid_transition(running_like, JOB_STATUS_CANCELLED) is False


class TestTerminalJobsCannotReenterActiveStatuses:
    @pytest.mark.parametrize("terminal", TERMINAL_JOB_STATUSES)
    @pytest.mark.parametrize("active", ACTIVE_JOB_STATUSES)
    def test_no_terminal_to_active_transition_is_valid(self, terminal: str, active: str) -> None:
        assert is_valid_transition(terminal, active) is False

    @pytest.mark.parametrize("terminal", TERMINAL_JOB_STATUSES)
    def test_terminal_statuses_have_no_outgoing_transitions(self, terminal: str) -> None:
        assert ALLOWED_TRANSITIONS[terminal] == ()

    def test_terminal_statuses_cannot_move_to_a_different_terminal_status(self) -> None:
        assert is_valid_transition(JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED) is False
        assert is_valid_transition(JOB_STATUS_FAILED, JOB_STATUS_CANCELLED) is False
        assert is_valid_transition(JOB_STATUS_CANCELLED, JOB_STATUS_SUCCEEDED) is False


class TestRepeatedCancellationIsIdempotent:
    def test_repeated_cancel_request_while_already_cancel_requested_is_a_no_op(self) -> None:
        assert is_valid_transition(JOB_STATUS_CANCEL_REQUESTED, JOB_STATUS_CANCEL_REQUESTED) is True

    def test_repeated_cancel_request_against_an_already_cancelled_job_is_a_no_op(self) -> None:
        assert is_valid_transition(JOB_STATUS_CANCELLED, JOB_STATUS_CANCELLED) is True

    @pytest.mark.parametrize("status", JOB_STATUSES)
    def test_every_status_allows_the_identity_transition(self, status: str) -> None:
        # Re-requesting the status a job is already in must never be treated
        # as an invalid transition, regardless of which status it is.
        assert is_valid_transition(status, status) is True


def test_normal_lifecycle_progression_is_valid() -> None:
    assert is_valid_transition(JOB_STATUS_QUEUED, JOB_STATUS_PREPARING) is True
    assert is_valid_transition(JOB_STATUS_PREPARING, JOB_STATUS_RUNNING) is True
    assert is_valid_transition(JOB_STATUS_RUNNING, JOB_STATUS_POSTPROCESSING) is True
    assert is_valid_transition(JOB_STATUS_POSTPROCESSING, JOB_STATUS_SUCCEEDED) is True


@pytest.mark.parametrize("running_like", RUNNING_LIKE_STATUSES)
def test_running_like_statuses_can_still_fail_directly(running_like: str) -> None:
    # Cancellation is not the only way an in-flight job ends; unrelated
    # generation errors must still be able to fail it directly.
    assert is_valid_transition(running_like, JOB_STATUS_FAILED) is True


def test_queued_can_fail_directly() -> None:
    assert is_valid_transition(JOB_STATUS_QUEUED, JOB_STATUS_FAILED) is True


def test_lifecycle_progression_cannot_skip_stages() -> None:
    assert is_valid_transition(JOB_STATUS_QUEUED, JOB_STATUS_RUNNING) is False
    assert is_valid_transition(JOB_STATUS_QUEUED, JOB_STATUS_POSTPROCESSING) is False
    assert is_valid_transition(JOB_STATUS_QUEUED, JOB_STATUS_SUCCEEDED) is False
    assert is_valid_transition(JOB_STATUS_PREPARING, JOB_STATUS_POSTPROCESSING) is False
    assert is_valid_transition(JOB_STATUS_PREPARING, JOB_STATUS_SUCCEEDED) is False
    assert is_valid_transition(JOB_STATUS_RUNNING, JOB_STATUS_SUCCEEDED) is False


def test_generation_status_literal_accepts_cancel_requested() -> None:
    """core/schemas/generation.py must accept the new status end-to-end."""

    from datetime import datetime, timezone

    from core.jobs.schemas import JobRecord
    from core.schemas import GenerationRequest

    request = GenerationRequest(
        media_type="video",
        prompt="a cancel-requested job",
        model_id="template-storyboard",
    )
    now = datetime.now(timezone.utc)
    record = JobRecord(
        id="job_test_cancel_requested",
        media_type="video",
        status=JOB_STATUS_CANCEL_REQUESTED,
        request=request,
        created_at=now,
        updated_at=now,
    )
    assert record.status == JOB_STATUS_CANCEL_REQUESTED
