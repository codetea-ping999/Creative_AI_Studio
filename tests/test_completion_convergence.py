"""PR3: deterministic regressions for `core.jobs.completion.CompletionConverger`.

No sleep-based waits: every scenario seeds a succeeded Job directly and
calls `converge_job()` (or fires a real `EventBus.publish()`) synchronously.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Event

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


# --- PR3 exact-HEAD audit, second round, P2-1: exclude outputless jobs
# from replay winner selection -----------------------------------------------


def test_outputless_candidate_never_wins_over_a_usable_older_one(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    older_usable = _seed_succeeded_job(
        job_repository, "job_older", created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        params=params, outputs=("older.png",),
    )
    newer_outputless = _seed_succeeded_job(
        job_repository, "job_newer", created_at=datetime.now(timezone.utc),
        params=params, outputs=(),
    )

    # Converge the outputless (but chronologically newer) one first -- it
    # can never produce an Asset, so it stays retryable on its own; this is
    # unrelated to the bug and unaffected by the fix.
    assert converger.converge_job(newer_outputless.id) == CompletionOutcome.RETRYABLE_FAILURE

    # The usable older candidate must still win the role -- not lose a
    # "newest wins" race to a candidate that can never actually fill it.
    outcome_older = converger.converge_job(older_usable.id)
    assert outcome_older == CompletionOutcome.DONE

    bound = story_repository.get(story.id)
    older_asset = asset_repository.get_primary_by_job(older_usable.id)
    assert older_asset is not None
    assert bound.scenes[0].asset_ids.get("visual") == older_asset.id

    # Converging the outputless one again must not disturb the binding --
    # the role-already-resolved check now finds it resolved.
    assert converger.converge_job(newer_outputless.id) == CompletionOutcome.DONE
    bound_after = story_repository.get(story.id)
    assert bound_after.scenes[0].asset_ids.get("visual") == older_asset.id


def test_outputless_candidate_never_wins_regardless_of_processing_order(tmp_path):
    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    older_usable = _seed_succeeded_job(
        job_repository, "job_older", created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        params=params, outputs=("older.png",),
    )
    newer_outputless = _seed_succeeded_job(
        job_repository, "job_newer", created_at=datetime.now(timezone.utc),
        params=params, outputs=(),
    )

    # Converge the usable OLDER one first this time -- the outputless newer
    # sibling must still never have been considered a viable winner.
    assert converger.converge_job(older_usable.id) == CompletionOutcome.DONE
    assert converger.converge_job(newer_outputless.id) == CompletionOutcome.DONE

    bound = story_repository.get(story.id)
    older_asset = asset_repository.get_primary_by_job(older_usable.id)
    assert bound.scenes[0].asset_ids.get("visual") == older_asset.id


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


# --- PR3 exact-HEAD audit, eighth round, finding 3: distinguish Story
# stat failures from deletion -------------------------------------------------


def test_story_get_for_recovery_treats_a_stat_failure_as_unreadable_not_missing(
    tmp_path, monkeypatch
):
    """`get_for_recovery()` now calls the story file's own `.stat()`
    directly and must distinguish a confirmed `FileNotFoundError` from
    any other transient `OSError` (a permission hiccup, a mount
    timeout) -- the latter must be reported as unreadable, never as
    confirmed absence, identically to the analogous Batch-side bug
    fixed the same round, but for a Story file (PR3 exact-HEAD audit,
    eighth round, finding 3). This is distinct from the read-failure
    case right above: here `.stat()` itself never gets far enough to
    attempt `read_text()` at all.

    Injects the failure by patching `pathlib.Path.stat` itself (the
    exact method `get_for_recovery()` calls), rather than the
    lower-level `os.stat()` free function: `Path.stat()`'s internal
    routing to `os.stat()` is a CPython-version-specific implementation
    detail (confirmed to differ between the local 3.14 environment and
    CI's Python 3.10 runtime -- an `os.stat()`-level patch does not
    reliably intercept every version's call path), while `Path.stat`
    itself is the stable, version-portable interception point that
    matches what the fixed production code actually calls.
    """

    story_repository = StoryRepository(tmp_path / "stories")
    story = story_repository.create(title="t")
    target_file = tmp_path / "stories" / f"{story.id}.json"

    real_path_stat = Path.stat

    def flaky_path_stat(self, *args, **kwargs):
        if self == target_file:
            raise OSError("injected: transient stat failure")
        return real_path_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_path_stat)

    result, confirmed_absent = story_repository.get_for_recovery(story.id)

    assert result is None
    assert confirmed_absent is False  # NOT confirmed absent -- must stay retryable

    monkeypatch.undo()  # the storage hiccup clears

    result_after, confirmed_absent_after = story_repository.get_for_recovery(story.id)
    assert result_after is not None
    assert result_after.id == story.id
    assert confirmed_absent_after is False


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


# --- PR3 exact-HEAD audit, fifth round, finding 1: treat malformed batch
# files as uncertain owners, not confirmed-absent ones -----------------------


def test_malformed_owning_batch_keeps_completion_pending_not_done(tmp_path):
    """A terminal child's owning Batch file being malformed (not merely
    transiently unreadable) must be treated exactly like a transient read
    failure by `reconcile_child_job()` -- `RETRYABLE_FAILURE`, never the
    confirmed-absent `NO_PARENT` outcome -- since a malformed file's
    content (including whether it actually owns this `job_id`) is just
    as unknown as an unreadable one's.

    Without this, `find_by_job_id_or_diagnose()` discarded evidence of a
    malformed candidate and reported `uncertain=False`, so a terminal
    child whose real owning Batch happened to be malformed right now was
    marked completion `done` and permanently excluded from every future
    retry -- even once the file was repaired, leaving a multi-stage
    Batch stuck on its old stage forever (PR3 exact-HEAD audit, fifth
    round, finding 1).
    """
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
        BatchSpec(
            name="malformed-owner", media_type="image", model_id="fake", prompt="x", limit=1,
            stages=[{"name": "probe"}, {"name": "refine"}],
        )
    )
    job_id = batch.items[0].job_id
    job_repository.update_status(job_id, "preparing")
    job_repository.update_status(job_id, "running")
    job_repository.update_status(job_id, "postprocessing")
    job_repository.update(
        job_id, status="succeeded", progress=1.0,
        result=GenerationResult(job_id=job_id, status="succeeded", outputs=["a.png"]),
    )

    batch_file = tmp_path / "batches" / f"{batch.id}.json"
    good_content = batch_file.read_text(encoding="utf-8")
    batch_file.write_text("{not valid json", encoding="utf-8")

    outcome = converger.converge_job(job_id)

    assert outcome == CompletionOutcome.RETRYABLE_FAILURE
    after = job_repository.get(job_id)
    assert after.completion_state == "pending"
    assert after.completion_error is not None
    assert after.status == "succeeded"  # the generation-level outcome is untouched

    # Repair the file -- the next retry finds the real owner and advances
    # its stage normally, exactly as if the file had never been malformed.
    batch_file.write_text(good_content, encoding="utf-8")

    second = converger.converge_job(job_id)

    assert second == CompletionOutcome.DONE
    assert job_repository.get(job_id).completion_state == "done"
    refreshed = batch_repository.get(batch.id)
    assert refreshed.stage_index == 1
    refine_item = next(item for item in refreshed.items if item.stage_index == 1)
    assert refine_item.job_id is not None
    assert job_repository.get(refine_item.job_id) is not None


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


# --- PR3 exact-HEAD audit, second round, P1-2: propagate failures from the
# stage-enqueue phase --------------------------------------------------------


def test_transient_stage_enqueue_failure_keeps_completion_pending_not_done(
    tmp_path, monkeypatch
):
    """The stage *advance* itself can persist successfully while the
    subsequent `_enqueue_stage()` call -- materializing the new stage's
    children -- fails on its own transient read. `reconcile_child_job()`
    must not report `RECONCILED` (and thus let completion be marked done)
    just because the advance half succeeded.
    """

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
        BatchSpec(
            name="two-stage", media_type="image", model_id="fake", prompt="x", limit=1,
            stages=[{"name": "probe"}, {"name": "refine"}],
        )
    )
    job_id = batch.items[0].job_id
    job_repository.update_status(job_id, "preparing")
    job_repository.update_status(job_id, "running")
    job_repository.update_status(job_id, "postprocessing")
    job_repository.update(
        job_id, status="succeeded", progress=1.0,
        result=GenerationResult(job_id=job_id, status="succeeded", outputs=["a.png"]),
    )

    # Injected transient OSError, surgically scoped: _try_load() backs
    # _enqueue_stage()'s own `mutate()` call (get() -> _try_load()), while
    # the stage-advance step uses the separate `_try_load_diagnosed()` path
    # (via `mutate_by_job_id_diagnosed()` -> `list_all_tolerant()`) and is
    # left untouched -- so the advance genuinely persists and only the
    # *following* stage-materialization read fails.
    original_try_load = batch_repository._try_load

    def flaky_try_load(batch_file):
        if batch_file.stem == batch.id:
            return None  # what _try_load() returns after catching an OSError
        return original_try_load(batch_file)

    monkeypatch.setattr(batch_repository, "_try_load", flaky_try_load)

    outcome = converger.converge_job(job_id)

    assert outcome == CompletionOutcome.RETRYABLE_FAILURE
    after = job_repository.get(job_id)
    assert after.completion_state == "pending"
    assert after.completion_error is not None

    # Confirm the advance really did persist despite the injected failure --
    # a direct (untouched-path) read shows the new stage exists with its
    # child not yet materialized.
    monkeypatch.undo()
    advanced_only = batch_repository.get(batch.id)
    assert advanced_only.stage_index == 1
    refine_item = next(item for item in advanced_only.items if item.stage_index == 1)
    assert refine_item.job_id is None

    second = converger.converge_job(job_id)

    assert second == CompletionOutcome.DONE
    assert job_repository.get(job_id).completion_state == "done"
    materialized = batch_repository.get(batch.id)
    refine_item_after = next(item for item in materialized.items if item.stage_index == 1)
    assert refine_item_after.job_id is not None
    assert job_repository.get(refine_item_after.job_id) is not None


# --- PR3 exact-HEAD audit, sixth round, finding 2: avoid rescanning every
# job for each replay candidate -----------------------------------------------


def test_shared_candidate_index_preserves_newest_usable_winner_semantics(tmp_path):
    """Passing an explicitly pre-built `SceneCandidateIndex` (the shared-
    index path a batch caller uses) must select exactly the same winner
    as the lazily-built-per-call path already covers elsewhere in this
    file -- the optimization changes *cost*, never the outcome.
    """
    from core.story.replay_selection import build_scene_candidate_index

    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    older_usable = _seed_succeeded_job(
        job_repository, "job_older", created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        params=params, outputs=("older.png",),
    )
    newer_outputless = _seed_succeeded_job(
        job_repository, "job_newer_outputless", created_at=datetime.now(timezone.utc),
        params=params, outputs=(),
    )
    # A handful of unrelated succeeded jobs targeting different scenes --
    # confirms the shared index does not let unrelated jobs leak into this
    # scene/role's own candidate list.
    for i in range(5):
        other_story = story_repository.create(title=f"Other {i}", premise="p")
        other_story = story_repository.save(apply_text_result(other_story, "scene_list", _SCENES))
        _seed_succeeded_job(
            job_repository, f"job_unrelated_{i}",
            params=scene_binding_params(other_story.id, other_story.scenes[0].id, "visual"),
            outputs=(f"unrelated_{i}.png",),
        )

    shared_index = build_scene_candidate_index(job_repository)

    assert converger.converge_job(
        newer_outputless.id, candidate_index=shared_index
    ) == CompletionOutcome.RETRYABLE_FAILURE
    assert converger.converge_job(
        older_usable.id, candidate_index=shared_index
    ) == CompletionOutcome.DONE

    bound = story_repository.get(story.id)
    older_asset = asset_repository.get_primary_by_job(older_usable.id)
    assert older_asset is not None
    assert bound.scenes[0].asset_ids.get("visual") == older_asset.id

    # Re-converging the outputless one with the SAME (now slightly stale
    # relative to the just-applied binding, but still-valid-for-candidate-
    # enumeration) shared index must still see the role as resolved --
    # that check reads the live Story fresh, never the cached index.
    assert converger.converge_job(
        newer_outputless.id, candidate_index=shared_index
    ) == CompletionOutcome.DONE


def test_shared_candidate_index_preserves_poison_conservatism(tmp_path):
    """The shared-index path must preserve the exact same conservative
    behavior as the per-call path: a relevant, undecodable succeeded
    sibling must still block an older decodable candidate from binding,
    never silently ignored just because candidate lookup was batched.
    """
    from core.story.replay_selection import build_scene_candidate_index

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
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", newer.id))
        raw.commit()

    shared_index = build_scene_candidate_index(job_repository)

    outcome = converger.converge_job(older.id, candidate_index=shared_index)

    assert outcome == CompletionOutcome.UNRESOLVED
    bound = story_repository.get(story.id)
    assert bound.scenes[0].asset_ids.get("visual") is None

    # Repair, rebuild the index fresh (matching how a real batch caller
    # rebuilds it every pass), and confirm normal convergence resumes.
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (newer.created_at.isoformat(), newer.id),
        )
        raw.commit()
    rebuilt_index = build_scene_candidate_index(job_repository)
    assert converger.converge_job(newer.id, candidate_index=rebuilt_index) == CompletionOutcome.DONE
    assert converger.converge_job(older.id, candidate_index=rebuilt_index) == CompletionOutcome.DONE
    bound_after = story_repository.get(story.id)
    newer_asset = asset_repository.get_primary_by_job(newer.id)
    assert bound_after.scenes[0].asset_ids.get("visual") == newer_asset.id


def test_shared_candidate_index_never_lets_an_unrelated_poison_row_taint_everything(
    tmp_path,
):
    """An ordinary poison row belonging to a non-scene-bound job (the
    overwhelmingly common case -- any plain image/audio/video job's
    `params` has no `story_id`/`scene_id`/`scene_role` at all) must never
    taint the *entire* index just because its own `params` happen to lack
    those keys (or hold a wrong-typed value). The old per-call
    implementation compared such a row's `(None, None, None)`-shaped
    tuple by strict equality against the specific query's guaranteed-
    all-`str` key -- which can never match -- so it was silently ignored
    for every query.

    Found via adversarial review of this round's own first attempt at a
    mypy fix for a *different* concern (a poison row whose params values
    are wrong-typed): that version conflated "keys missing/wrong-typed"
    with "payload could not be parsed at all" and set the global
    `has_unattributable_poison_row` flag for both -- which, applied to
    this ubiquitous case, would have permanently blocked Story-replay
    convergence platform-wide the moment any single unrelated job's row
    became undecodable for any reason at all.
    """
    from core.story.replay_selection import build_scene_candidate_index

    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    usable = _seed_succeeded_job(
        job_repository, "job_usable", params=params, outputs=("out.png",),
    )
    # A completely unrelated, ordinary (non-scene-bound) job whose row is
    # independently undecodable via list_tolerant() (corrupt created_at)
    # -- its own params have no story_id/scene_id/scene_role at all,
    # exactly like any real ad-hoc generation request.
    unrelated = _seed_succeeded_job(
        job_repository, "job_unrelated", params={"prompt": "a cat"}, outputs=("cat.png",),
    )
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", unrelated.id)
        )
        raw.commit()

    index = build_scene_candidate_index(job_repository)

    assert index.has_unattributable_poison_row is False
    outcome = converger.converge_job(usable.id, candidate_index=index)
    assert outcome == CompletionOutcome.DONE
    bound = story_repository.get(story.id)
    usable_asset = asset_repository.get_primary_by_job(usable.id)
    assert bound.scenes[0].asset_ids.get("visual") == usable_asset.id


def test_shared_candidate_index_ignores_a_poison_row_with_wrongly_typed_params(tmp_path):
    """A poison row whose `params` decode fine but hold a non-string
    value in `story_id`/`scene_id`/`scene_role` can never match any real
    `(str, str, str)` query key -- same reasoning as the "missing keys"
    case above -- so it must not taint the index either, matching the
    old per-call implementation's own strict-equality behavior exactly.
    """
    from core.story.replay_selection import build_scene_candidate_index

    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    story = _create_bound_story(story_repository)
    scene_id = story.scenes[0].id
    params = scene_binding_params(story.id, scene_id, "visual")

    usable = _seed_succeeded_job(
        job_repository, "job_usable", params=params, outputs=("out.png",),
    )
    weird = _seed_succeeded_job(
        job_repository, "job_weird",
        params={"story_id": 12345, "scene_id": scene_id, "scene_role": "visual"},
        outputs=("weird.png",),
    )
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE jobs SET created_at = ? WHERE id = ?", ("not-a-timestamp", weird.id))
        raw.commit()

    index = build_scene_candidate_index(job_repository)

    assert index.has_unattributable_poison_row is False
    assert (story.id, scene_id, "visual") not in index.poisoned_keys
    outcome = converger.converge_job(usable.id, candidate_index=index)
    assert outcome == CompletionOutcome.DONE


def test_idle_startup_recovery_builds_no_candidate_index(tmp_path, monkeypatch):
    """The same "no completion work, no candidate index" contract applies
    to `run_startup_recovery()`'s step 4, not just the runtime retry
    loop: a fresh/already-converged database has nothing for step 4's
    loop to do at all, and must not pay for a full-table scan anyway.
    """
    from core.jobs import JobQueue, JobService
    from core.jobs.startup_recovery import run_startup_recovery
    import core.story.replay_selection as replay_selection_module

    job_repository, asset_repository, story_repository, scene_binder, converger = _build(
        tmp_path
    )
    job_service = JobService(job_repository, JobQueue())

    for i in range(20):
        job = _seed_succeeded_job(job_repository, f"job_done_{i}")
        job_repository.mark_completion_done(job.id)
    assert job_repository.list_terminal_pending_completion() == []

    build_calls = {"count": 0}
    real_build = replay_selection_module.build_scene_candidate_index

    def counting_build(job_repository_arg):
        build_calls["count"] += 1
        return real_build(job_repository_arg)

    monkeypatch.setattr(replay_selection_module, "build_scene_candidate_index", counting_build)

    report = run_startup_recovery(job_repository, job_service, converger)

    assert build_calls["count"] == 0
    assert report.completion_outcomes == {}


def test_startup_recovery_builds_the_scene_candidate_index_at_most_once_per_pass(
    tmp_path, monkeypatch
):
    """N scene-bound succeeded jobs, each targeting a DIFFERENT scene (so
    no single-key cache hit could mask the fix), must not each trigger
    their own full Story-replay-candidate scan during one startup
    recovery pass -- before this fix, that made a legacy-migration
    backlog's startup cost effectively O(N^2): N pending jobs times one
    full-table `list_tolerant()` scan+decode each (PR3 exact-HEAD audit,
    sixth round, finding 2).
    """
    from core.jobs import JobQueue, JobService
    from core.jobs.startup_recovery import run_startup_recovery
    import core.story.replay_selection as replay_selection_module

    job_repository, asset_repository, story_repository, scene_binder, converger = _build(tmp_path)
    job_service = JobService(job_repository, JobQueue())

    job_count = 20
    expected_asset_ids = {}
    for i in range(job_count):
        story = story_repository.create(title=f"Story {i}", premise="p")
        story = story_repository.save(apply_text_result(story, "scene_list", _SCENES))
        scene_id = story.scenes[0].id
        params = scene_binding_params(story.id, scene_id, "visual")
        job = _seed_succeeded_job(
            job_repository, f"job_{i}", params=params, outputs=(f"out_{i}.png",)
        )
        expected_asset_ids[job.id] = story.id

    real_build = replay_selection_module.build_scene_candidate_index
    call_count = {"count": 0}

    def counting_build(job_repository_arg):
        call_count["count"] += 1
        return real_build(job_repository_arg)

    monkeypatch.setattr(replay_selection_module, "build_scene_candidate_index", counting_build)

    report = run_startup_recovery(job_repository, job_service, converger)

    # Built once for this whole pass -- not once per job.
    assert call_count["count"] <= 1
    assert len(report.completion_outcomes) == job_count
    for job_id in expected_asset_ids:
        assert job_repository.get(job_id).completion_state == "done"
        outcome = report.completion_outcomes[job_id]
        assert outcome == CompletionOutcome.DONE


# --- PR3 exact-HEAD audit, seventh round, finding 2: skip candidate scans
# when no completion work exists ----------------------------------------------


def test_idle_retry_tick_builds_no_candidate_index_and_scans_nothing(
    tmp_path, monkeypatch
):
    """An idle system (a large already-converged job history, nothing
    currently completion-pending) must not build a `SceneCandidateIndex`
    -- and therefore must not perform its full-table `list_tolerant()`
    scan -- on any given `run_retry_loop()` tick.

    Before this fix, the index was built unconditionally every tick
    regardless of whether there was any completion work to do at all --
    on an otherwise-idle system with a large job history, that is a full
    O(N) scan+decode every `poll_interval_seconds` forever, silently
    defeating the whole point of `list_terminal_pending_completion()`'s
    own supporting SQLite index (PR3 exact-HEAD audit, seventh round,
    finding 2).
    """
    import core.story.replay_selection as replay_selection_module

    job_repository, asset_repository, story_repository, scene_binder, converger = _build(
        tmp_path
    )

    # A "large" job history, all already fully converged.
    for i in range(20):
        job = _seed_succeeded_job(job_repository, f"job_done_{i}")
        job_repository.mark_completion_done(job.id)
    assert job_repository.list_terminal_pending_completion() == []

    build_calls = {"count": 0}
    real_build = replay_selection_module.build_scene_candidate_index

    def counting_build(job_repository_arg):
        build_calls["count"] += 1
        return real_build(job_repository_arg)

    monkeypatch.setattr(replay_selection_module, "build_scene_candidate_index", counting_build)

    scan_calls = {"count": 0}
    original_list_tolerant = job_repository.list_tolerant

    def counting_list_tolerant():
        scan_calls["count"] += 1
        return original_list_tolerant()

    monkeypatch.setattr(job_repository, "list_tolerant", counting_list_tolerant)

    # Run exactly one tick, deterministically: the stop_event is set as a
    # side effect of this tick's own list_terminal_pending_completion()
    # call, so Event.wait() below returns immediately with no real sleep,
    # and the loop's own `while not stop_event.is_set()` check then exits
    # before a second tick ever starts.
    stop_event = Event()
    original_list_pending = job_repository.list_terminal_pending_completion

    def list_pending_then_stop():
        result = original_list_pending()
        stop_event.set()
        return result

    monkeypatch.setattr(
        job_repository, "list_terminal_pending_completion", list_pending_then_stop
    )

    converger.run_retry_loop(stop_event=stop_event, poll_interval_seconds=0)

    assert build_calls["count"] == 0
    assert scan_calls["count"] == 0


def test_active_retry_tick_builds_the_candidate_index_at_most_once(tmp_path, monkeypatch):
    """N genuinely completion-pending jobs on one tick must still build
    the shared `SceneCandidateIndex` exactly once for that tick -- the
    "skip when idle" fix (see the test above) must not regress the
    "shared, not per-job" fix from the prior round.
    """
    import core.story.replay_selection as replay_selection_module

    job_repository, asset_repository, story_repository, scene_binder, converger = _build(
        tmp_path
    )

    job_count = 10
    for i in range(job_count):
        story = story_repository.create(title=f"Story {i}", premise="p")
        story = story_repository.save(apply_text_result(story, "scene_list", _SCENES))
        params = scene_binding_params(story.id, story.scenes[0].id, "visual")
        _seed_succeeded_job(job_repository, f"job_{i}", params=params, outputs=(f"out_{i}.png",))
    assert len(job_repository.list_terminal_pending_completion()) == job_count

    build_calls = {"count": 0}
    real_build = replay_selection_module.build_scene_candidate_index

    def counting_build(job_repository_arg):
        build_calls["count"] += 1
        return real_build(job_repository_arg)

    monkeypatch.setattr(replay_selection_module, "build_scene_candidate_index", counting_build)

    # Stop only after every pending job this tick has actually been
    # converged -- unlike the idle test above, setting stop_event any
    # earlier (e.g. right after listing pending jobs) would trip the
    # loop's own graceful-shutdown check (`if stop_event.is_set(): break`)
    # partway through this same tick's for-loop, converging none of them.
    stop_event = Event()
    convergence_calls = {"count": 0}
    original_converge_job = converger.converge_job

    def converge_job_then_maybe_stop(job_id, **kwargs):
        result = original_converge_job(job_id, **kwargs)
        convergence_calls["count"] += 1
        if convergence_calls["count"] >= job_count:
            stop_event.set()
        return result

    monkeypatch.setattr(converger, "converge_job", converge_job_then_maybe_stop)

    converger.run_retry_loop(stop_event=stop_event, poll_interval_seconds=0)

    assert build_calls["count"] == 1
    assert convergence_calls["count"] == job_count
    for i in range(job_count):
        assert job_repository.get(f"job_{i}").completion_state == "done"
