"""PR3: deterministic regressions for `core.jobs.completion.CompletionConverger`.

No sleep-based waits: every scenario seeds a succeeded Job directly and
calls `converge_job()` (or fires a real `EventBus.publish()`) synchronously.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

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


# --- PR3 exact-HEAD audit P2-1: undecodable candidates block a winner -----


def test_undecodable_newer_candidate_prevents_an_older_one_from_binding(tmp_path):
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
    # Corrupt the newer candidate's created_at so list_tolerant() can no
    # longer fully decode it -- but leave its status/request_json (and
    # therefore its params) intact, matching the exact-HEAD audit's
    # scenario: a succeeded row broken in a column irrelevant to relevance
    # detection, not a broken request payload.
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", newer.id))
        raw.commit()

    outcome = converger.converge_job(older.id)

    assert outcome == CompletionOutcome.UNRESOLVED
    after = job_repository.get(older.id)
    assert after.completion_state == "pending"
    bound = story_repository.get(story.id)
    assert bound.scenes[0].asset_ids.get("visual") is None

    # Repair the newer row -- it legitimately wins once decodable again,
    # and the older one converges too, without ever having bound the role.
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (newer.created_at.isoformat(), newer.id),
        )
        raw.commit()
    assert converger.converge_job(newer.id) == CompletionOutcome.DONE
    assert converger.converge_job(older.id) == CompletionOutcome.DONE
    bound_after = story_repository.get(story.id)
    newer_asset = asset_repository.get_primary_by_job(newer.id)
    assert bound_after.scenes[0].asset_ids.get("visual") == newer_asset.id


# --- PR3 exact-HEAD audit P1-4: an unreadable Story is not a deleted one --


def test_transient_story_read_failure_keeps_completion_pending_not_done(tmp_path, monkeypatch):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    job = _seed_succeeded_job(
        job_repository, "job_a", params=scene_binding_params(story.id, scene_id, "visual"),
    )

    monkeypatch.setattr(story_repository, "get_for_recovery", lambda story_id: (None, False))

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.RETRYABLE_FAILURE
    after = job_repository.get(job.id)
    assert after.completion_state == "pending"
    unchanged = story_repository.get(story.id)
    assert unchanged.scenes[0].asset_ids == {}  # not resurrected, not touched

    monkeypatch.undo()

    assert converger.converge_job(job.id) == CompletionOutcome.DONE
    bound = story_repository.get(story.id)
    assert bound.scenes[0].asset_ids.get("visual") is not None


def test_malformed_story_file_keeps_completion_pending_not_done(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    job = _seed_succeeded_job(
        job_repository, "job_a", params=scene_binding_params(story.id, scene_id, "visual"),
    )
    story_file = tmp_path / "stories" / f"{story.id}.json"
    original_content = story_file.read_text(encoding="utf-8")
    story_file.write_text("{not valid json", encoding="utf-8")

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.RETRYABLE_FAILURE
    assert job_repository.get(job.id).completion_state == "pending"

    story_file.write_text(original_content, encoding="utf-8")

    assert converger.converge_job(job.id) == CompletionOutcome.DONE


def test_story_get_for_recovery_distinguishes_missing_from_unreadable(tmp_path):
    story_repository = StoryRepository(tmp_path / "stories")
    story = story_repository.create(title="t")

    missing, confirmed_absent = story_repository.get_for_recovery("story_does_not_exist")
    assert missing is None
    assert confirmed_absent is True

    story_file = tmp_path / "stories" / f"{story.id}.json"
    original_content = story_file.read_text(encoding="utf-8")
    story_file.write_text("{not valid json", encoding="utf-8")
    broken, confirmed_absent_2 = story_repository.get_for_recovery(story.id)
    assert broken is None
    assert confirmed_absent_2 is False

    story_file.write_text(original_content, encoding="utf-8")
    repaired, confirmed_absent_3 = story_repository.get_for_recovery(story.id)
    assert repaired is not None
    assert confirmed_absent_3 is False


def test_story_get_for_recovery_treats_a_read_error_as_unreadable_not_missing(
    tmp_path, monkeypatch
):
    story_repository = StoryRepository(tmp_path / "stories")
    story = story_repository.create(title="t")
    story_file = tmp_path / "stories" / f"{story.id}.json"
    original_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == story_file:
            raise OSError("injected transient read failure")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    result, confirmed_absent = story_repository.get_for_recovery(story.id)

    assert result is None
    assert confirmed_absent is False


# --- PR3 exact-HEAD audit P2-2: invalid scene roles never retry forever ---


def test_typo_scene_role_converges_instead_of_retrying_forever(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    # A direct API caller can put an arbitrary string in scene_role,
    # bypassing scene_binding_params()'s own validation (which raises for
    # this) -- simulating exactly that here.
    job = _seed_succeeded_job(
        job_repository, "job_a",
        params={"story_id": story.id, "scene_id": scene_id, "scene_role": "visaul"},
    )

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.DONE
    after = job_repository.get(job.id)
    assert after.completion_state == "done"
    # Retrying again must stay a safe no-op, never regress back to pending.
    assert converger.converge_job(job.id) == CompletionOutcome.SAFE_NOOP


# --- PR3 exact-HEAD audit P2-4: role-occupied check before requiring an Asset


def test_older_job_with_no_asset_converges_once_a_newer_candidate_already_bound(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    older = _seed_succeeded_job(
        job_repository, "job_older", created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        params=params, outputs=(),  # no outputs at all -- no Asset ever syncs for it
    )
    newer = _seed_succeeded_job(
        job_repository, "job_newer", created_at=datetime.now(timezone.utc),
        params=params, outputs=("newer.png",),
    )

    assert converger.converge_job(newer.id) == CompletionOutcome.DONE

    outcome = converger.converge_job(older.id)

    assert outcome == CompletionOutcome.DONE
    after = job_repository.get(older.id)
    assert after.completion_state == "done"


# --- PR3 exact-HEAD audit P1-5: Batch reconciliation must confirm success -


def test_transient_batch_read_failure_keeps_completion_pending_not_done(tmp_path, monkeypatch):
    from core.batches import BatchRepository, BatchService
    from core.batches.schemas import BatchSpec
    from core.jobs import JobQueue, JobService

    job_repository = JobRepository(tmp_path / "jobs.db")
    asset_repository = AssetRepository(tmp_path / "assets")
    batch_repository = BatchRepository(tmp_path / "batches")
    job_service = JobService(job_repository, JobQueue())
    batch_service = BatchService(batch_repository, job_service, job_repository)
    converger = CompletionConverger(job_repository, asset_repository, batch_service=batch_service)

    batch = batch_service.create_batch(
        BatchSpec(name="owner", media_type="image", model_id="fake", prompt="x", limit=1)
    )
    job_id = batch.items[0].job_id
    job_repository.update_status(job_id, "preparing")
    job_repository.update_status(job_id, "running")
    job_repository.update_status(job_id, "postprocessing")
    job_repository.update(
        job_id, status="succeeded", progress=1.0,
        result=GenerationResult(job_id=job_id, status="succeeded", outputs=["a.png"]),
    )

    original_try_load_diagnosed = batch_repository._try_load_diagnosed

    def flaky_try_load_diagnosed(batch_file):
        if batch_file.stem == batch.id:
            return None, True  # simulate a transient OSError reading this exact file
        return original_try_load_diagnosed(batch_file)

    monkeypatch.setattr(batch_repository, "_try_load_diagnosed", flaky_try_load_diagnosed)

    outcome = converger.converge_job(job_id)

    assert outcome == CompletionOutcome.RETRYABLE_FAILURE
    after = job_repository.get(job_id)
    assert after.completion_state == "pending"
    assert after.completion_error is not None
    assert after.status == "succeeded"  # the generation-level outcome is untouched

    monkeypatch.undo()

    second = converger.converge_job(job_id)

    assert second == CompletionOutcome.DONE
    assert job_repository.get(job_id).completion_state == "done"


def test_no_parent_batch_still_converges_to_done(tmp_path):
    from core.batches import BatchRepository, BatchService
    from core.jobs import JobQueue, JobService

    job_repository = JobRepository(tmp_path / "jobs.db")
    asset_repository = AssetRepository(tmp_path / "assets")
    batch_repository = BatchRepository(tmp_path / "batches")
    job_service = JobService(job_repository, JobQueue())
    batch_service = BatchService(batch_repository, job_service, job_repository)
    converger = CompletionConverger(job_repository, asset_repository, batch_service=batch_service)

    job = _seed_succeeded_job(job_repository, "job_standalone")

    outcome = converger.converge_job(job.id)

    assert outcome == CompletionOutcome.DONE
    assert job_repository.get(job.id).completion_state == "done"
