"""PR3: deterministic regressions for `core.jobs.completion.CompletionConverger`.

No sleep-based waits: every scenario seeds a succeeded Job directly and
calls `converge_job()` (or fires a real `EventBus.publish()`) synchronously.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.assets import AssetRepository
from core.jobs import EventBus
from core.jobs.completion import CompletionConverger, CompletionOutcome
from core.jobs.schemas import JobRecord
from core.schemas import GenerationRequest, GenerationResult
from core.storage.repositories.job_repository import JobRepository
from core.story import SceneBinder, StoryRepository, apply_text_result, scene_binding_params

_SCENES = {
    "scenes": [
        {
            "heading": "rooftop at dawn",
            "narration": "the light rose over the city.",
            "image_prompt": "rooftop at dawn",
            "bgm_mood": "hopeful",
            "duration_seconds": 4,
        },
    ]
}


def _seed_succeeded_job(repository, job_id, *, created_at=None, params=None, outputs=("a.png",)):
    now = created_at or datetime.now(timezone.utc)
    return repository.create(
        JobRecord(
            id=job_id,
            status="succeeded",
            media_type="image",
            request=GenerationRequest(
                media_type="image", prompt="fake", model_id="fake", params=params or {}
            ),
            result=GenerationResult(job_id=job_id, status="succeeded", outputs=list(outputs)),
            created_at=now,
            updated_at=now,
        )
    )


def _build(tmp_path, *, event_bus=None):
    job_repository = JobRepository(tmp_path / "jobs.db")
    asset_repository = AssetRepository(tmp_path / "assets")
    story_repository = StoryRepository(tmp_path / "stories")
    scene_binder = SceneBinder(
        story_repository, job_repository, asset_repository, event_bus=event_bus
    )
    converger = CompletionConverger(
        job_repository, asset_repository,
        story_repository=story_repository, scene_binder=scene_binder,
    )
    return job_repository, asset_repository, story_repository, scene_binder, converger


def _create_bound_story(story_repository, scene_id="scene_01", role="visual"):
    story = story_repository.create(title="Rewind", premise="p")
    story = story_repository.save(apply_text_result(story, "scene_list", _SCENES))
    return story


# --- Cases 8/9: Asset sync failure, then retry ----------------------------


def test_asset_sync_failure_leaves_completion_pending(tmp_path, monkeypatch):
    job_repository, asset_repository, *_rest, converger = _build(tmp_path)
    job = _seed_succeeded_job(job_repository, "job_a")

    def fail_sync(_job):
        raise OSError("injected asset sync failure")

    monkeypatch.setattr(asset_repository, "sync_job", fail_sync)

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.RETRYABLE_FAILURE
    after = job_repository.get(job.id)
    assert after.completion_state == "pending"
    assert after.completion_error is not None
    assert "injected asset sync failure" in after.completion_error
    # The generation-level outcome must be untouched by a completion failure.
    assert after.status == "succeeded"
    assert after.error_message is None


def test_retry_after_asset_sync_recovers_to_done(tmp_path, monkeypatch):
    job_repository, asset_repository, *_rest, converger = _build(tmp_path)
    job = _seed_succeeded_job(job_repository, "job_a")

    calls = {"count": 0}
    original_sync = asset_repository.sync_job

    def flaky_sync(job_record):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("injected asset sync failure")
        return original_sync(job_record)

    monkeypatch.setattr(asset_repository, "sync_job", flaky_sync)

    first = converger.converge_job(job.id)
    second = converger.converge_job(job.id)

    assert first == CompletionOutcome.RETRYABLE_FAILURE
    assert second == CompletionOutcome.DONE
    after = job_repository.get(job.id)
    assert after.completion_state == "done"
    assert after.completion_error is None


# --- Case 10: Story replay precondition not met -> retryable --------------


def test_story_replay_without_an_asset_yet_is_retryable(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    # outputs=() -> AssetRepository.sync_job() persists nothing for this job,
    # so the Story-replay step's own Asset lookup finds none -- a genuine
    # "precondition not met yet", not a bug.
    job = _seed_succeeded_job(
        job_repository, "job_a",
        params=scene_binding_params(story.id, scene_id, "visual"),
        outputs=(),
    )

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.RETRYABLE_FAILURE
    after = job_repository.get(job.id)
    assert after.completion_state == "pending"
    assert after.completion_error is not None


# --- Case 11: already-applied Story -> safe completion ---------------------


def test_already_applied_story_binding_converges_as_a_safe_completion(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    job = _seed_succeeded_job(
        job_repository, "job_a",
        params=scene_binding_params(story.id, scene_id, "visual"),
    )

    first = converger.converge_job(job.id)
    second = converger.converge_job(job.id)  # already done -> SAFE_NOOP

    assert first == CompletionOutcome.DONE
    assert second == CompletionOutcome.SAFE_NOOP
    bound = story_repository.get(story.id)
    assert bound.scenes[0].asset_ids.get("visual") is not None


# --- Case 12: deleted Story/Scene -> no resurrection -----------------------


def test_deleted_story_converges_without_resurrecting_it(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    job = _seed_succeeded_job(
        job_repository, "job_a",
        params=scene_binding_params("story_missing", "scene_missing", "visual"),
    )

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.DONE
    assert job_repository.get(job.id).completion_state == "done"
    assert story_repository.get("story_missing") is None


def test_deleted_scene_converges_without_resurrecting_the_story(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    job = _seed_succeeded_job(
        job_repository, "job_a",
        params=scene_binding_params(story.id, "scene_no_longer_exists", "visual"),
    )

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.DONE
    unchanged = story_repository.get(story.id)
    assert unchanged.scenes[0].asset_ids == {}


# --- Case 13: newer role Asset exists -> old replay never overwrites ------


def test_older_succeeded_candidate_never_overwrites_a_newer_ones_binding(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    older = _seed_succeeded_job(
        job_repository, "job_older", created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        params=params, outputs=("older.png",),
    )
    newer = _seed_succeeded_job(
        job_repository, "job_newer", created_at=datetime.now(timezone.utc),
        params=params, outputs=("newer.png",),
    )

    # Converge the newer one first -- it should win the role.
    assert converger.converge_job(newer.id) == CompletionOutcome.DONE
    # The older one converges too (it must not error forever), but must not
    # overwrite what the newer job already bound.
    assert converger.converge_job(older.id) == CompletionOutcome.DONE

    bound = story_repository.get(story.id)
    newer_asset = asset_repository.get_primary_by_job(newer.id)
    assert bound.scenes[0].asset_ids.get("visual") == newer_asset.id


def test_candidate_selection_picks_the_newest_when_none_applied_yet(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    older = _seed_succeeded_job(
        job_repository, "job_older", created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        params=params, outputs=("older.png",),
    )
    newer = _seed_succeeded_job(
        job_repository, "job_newer", created_at=datetime.now(timezone.utc),
        params=params, outputs=("newer.png",),
    )

    # Converge the OLDER one first this time -- neither has been applied
    # yet, so candidate selection must pick the newer one as the winner
    # regardless of processing order, rather than "first writer wins".
    assert converger.converge_job(older.id) == CompletionOutcome.DONE
    assert converger.converge_job(newer.id) == CompletionOutcome.DONE

    bound = story_repository.get(story.id)
    newer_asset = asset_repository.get_primary_by_job(newer.id)
    assert bound.scenes[0].asset_ids.get("visual") == newer_asset.id


# --- Case 14: EventBus subscriber failure != completion done --------------


def test_eventbus_subscriber_failure_never_marks_completion_done(tmp_path, monkeypatch):
    event_bus = EventBus()
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(
        tmp_path, event_bus=event_bus
    )
    converger.attach_to_event_bus(event_bus)
    job = _seed_succeeded_job(job_repository, "job_a")

    def fail_sync(_job):
        raise RuntimeError("injected failure inside the event-driven convergence path")

    monkeypatch.setattr(asset_repository, "sync_job", fail_sync)

    # EventBus.publish() isolates each subscriber in its own try/except and
    # logs+swallows any exception (see core/jobs/events.py) -- this must
    # never be mistaken for "the subscriber completed successfully".
    event_bus.publish("job_succeeded", {"job_id": job.id})

    after = job_repository.get(job.id)
    assert after.completion_state == "pending"
    assert after.completion_error is not None
