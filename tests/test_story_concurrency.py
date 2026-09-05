"""Story lifecycle hardening (PR 2): a shared, atomic update boundary.

Several writers can touch the same ``StoryDocument`` in one process — the
story API's PATCH/apply/delete routes, ``SceneBinder``, and (in a future PR)
job recovery. Without a boundary shared by all of them, a plain
read-modify-write race loses whichever side saves first:

    A: API PATCH reads story S0
    B: SceneBinder reads the same S0
    B: sets visual=Asset-V, saves
    A: sets title="New" on its (stale) S0, saves the whole document
    -> title="New" persists, but B's visual assignment is gone

``StoryRepository.mutate()`` closes this by holding one lock across the read,
the caller's mutation, and the atomic save (see core/story/repository.py).
This module proves, deterministically and without any ``time.sleep()``-based
timing, that the boundary actually holds under concurrent writers, and that
``SceneBinder.replay_job_safely()`` -- a boundary kept deliberately separate
from the live ``bind_job()`` path -- is safe for a future recovery path to
reapply an old job's result without clobbering newer state.

Concurrency is driven entirely with ``threading.Event``/``threading.Barrier``
checkpoints: either the interleaving is forced explicitly (one side blocks at
a known point until the test releases it), or a ``Barrier`` lines two threads
up to attempt truly concurrent writes and the test asserts on the final,
order-independent state — never on the fact that this thread happened to run
before that one.
"""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.assets import AssetRepository  # noqa: E402
from core.jobs import EventBus, JobQueue, JobService  # noqa: E402
from core.schemas import GenerationRequest, GenerationResult  # noqa: E402
from core.storage.repositories.job_repository import JobRepository  # noqa: E402
from core.story import (  # noqa: E402
    SceneBinder,
    StoryRepository,
    apply_text_result,
    scene_binding_params,
)

try:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from bootstrap import create_application_services
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

_JOIN_TIMEOUT = 5.0

_SCENES = {
    "scenes": [
        {
            "heading": "屋上の朝",
            "narration": "朝の光が街を照らしていた。",
            "image_prompt": "rooftop at dawn",
            "bgm_mood": "hopeful",
            "duration_seconds": 4,
        },
    ]
}


class StoryRepositoryMutateTests(unittest.TestCase):
    """Repository-level proofs: requirements #2 and #8 of the PR 2 audit."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.repository = StoryRepository(Path(self._temporary.name) / "stories")

    def test_concurrent_field_updates_do_not_lose_either_change(self) -> None:
        story = self.repository.create(title="Old", genre="")
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _update(**fields: str) -> None:
            try:
                barrier.wait(timeout=_JOIN_TIMEOUT)
                self.repository.update(story.id, **fields)
            except BaseException as exc:  # noqa: BLE001 - surfaced via assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=_update, kwargs={"title": "New Title"}),
            threading.Thread(target=_update, kwargs={"genre": "Sci-Fi"}),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        final = self.repository.get(story.id)
        self.assertEqual(final.title, "New Title")
        self.assertEqual(final.genre, "Sci-Fi")

    def test_exception_in_mutation_leaves_story_unsaved_and_releases_the_lock(
        self,
    ) -> None:
        """read latest -> callback -> callback raises -> no save -> lock released.

        Each arrow above is asserted explicitly: the callback receives the
        current document (proving the read happened first), the exception
        propagates, nothing on disk changes (not even a stray temp file from
        ``write_json_atomic``), and -- checked from a second thread, since an
        RLock would let its own owning thread back in regardless of whether it
        was ever released -- the lock is free for the next caller.
        """

        story = self.repository.create(title="Original")
        seen: list[object] = []

        def _boom(current: object) -> None:
            seen.append(current)  # proves mutate() read-before-calling-back
            raise RuntimeError("boom")

        raised: list[BaseException] = []

        def _call_boom() -> None:
            try:
                self.repository.mutate(story.id, _boom)
            except RuntimeError as exc:
                raised.append(exc)

        thread = threading.Thread(target=_call_boom)
        thread.start()
        thread.join(timeout=_JOIN_TIMEOUT)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].title, "Original")
        self.assertEqual(len(raised), 1)

        # Nothing was saved: the document on disk is byte-for-byte what it
        # was before, and no leftover .tmp file was left behind either (the
        # exception happened before save() -- and therefore before
        # write_json_atomic() -- ever ran).
        self.assertEqual(self.repository.get(story.id).title, "Original")
        self.assertEqual(
            [entry.name for entry in self.repository.story_dir.iterdir()],
            [f"{story.id}.json"],
        )

        # Prove the lock was actually released for OTHER threads, not just
        # re-entered by the same one (an RLock lets its own owning thread back
        # in regardless, so that alone would not catch a release bug here).
        done = threading.Event()

        def _call_ok() -> None:
            self.repository.mutate(
                story.id, lambda current: current.model_copy(update={"title": "Fixed"})
            )
            done.set()

        thread2 = threading.Thread(target=_call_ok)
        thread2.start()
        thread2.join(timeout=_JOIN_TIMEOUT)

        self.assertTrue(done.is_set())
        self.assertEqual(self.repository.get(story.id).title, "Fixed")

    def test_mutate_does_not_resurrect_an_already_deleted_story(self) -> None:
        story = self.repository.create(title="Gone soon")
        self.assertTrue(self.repository.delete(story.id))

        result = self.repository.mutate(
            story.id, lambda current: current.model_copy(update={"title": "Back?"})
            if current is not None
            else None,
        )

        self.assertIsNone(result)
        self.assertIsNone(self.repository.get(story.id))


class SceneBinderConcurrencyTests(unittest.TestCase):
    """SceneBinder against the shared boundary: requirements #3, #5, #6, #7."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)

        self.job_repository = JobRepository(root / "jobs.db")
        self.asset_repository = AssetRepository(root / "assets")
        self.story_repository = StoryRepository(root / "stories")
        self.job_service = JobService(
            self.job_repository,
            JobQueue(),
            EventBus(),
            asset_repository=self.asset_repository,
        )
        self.binder = SceneBinder(
            self.story_repository, self.job_repository, self.asset_repository
        )

        story = self.story_repository.create(title="Rewind", premise="p")
        self.story = self.story_repository.save(
            apply_text_result(story, "scene_list", _SCENES)
        )

    def _succeed_scene_job(
        self, scene_id: str, role: str, *, output: str = "outputs/images/a.png"
    ) -> str:
        request = GenerationRequest(
            media_type="image",
            prompt="rooftop at dawn",
            model_id="sdxl",
            params=scene_binding_params(self.story.id, scene_id, role),
        )
        job = self.job_service.create_job(request)
        self.job_service.mark_succeeded(
            job.id,
            GenerationResult(
                job_id=job.id,
                status="succeeded",
                outputs=[output],
                previews=[output],
                metadata={},
            ),
        )
        return job.id

    def test_delete_vs_binder_never_resurrects_the_story(self) -> None:
        job_id = self._succeed_scene_job("scene_01", "visual")
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _delete() -> None:
            try:
                barrier.wait(timeout=_JOIN_TIMEOUT)
                self.story_repository.delete(self.story.id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _bind() -> None:
            try:
                barrier.wait(timeout=_JOIN_TIMEOUT)
                self.binder.bind_job(job_id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_delete), threading.Thread(target=_bind)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        # Whichever ran first, delete() always removes whatever file exists
        # at the moment it runs, and a concurrent bind_job() never recreates
        # a story mutate() found missing -- so the end state is deleted
        # either way.
        self.assertIsNone(self.story_repository.get(self.story.id))

    def test_bind_job_never_resurrects_after_a_completed_delete(self) -> None:
        """The exact failure mode named in the audit, with the order pinned down.

        ``bind_job`` never holds a story object across any wait: the only
        read it acts on is the one ``StoryRepository.mutate()`` takes for it,
        inside the lock, at the moment it actually runs. So once a delete has
        fully completed, there is no stale copy left anywhere for a
        subsequent bind to save back into existence -- this is a plain
        sequential proof of that, complementing the concurrent one above.
        """

        job_id = self._succeed_scene_job("scene_01", "visual")

        self.assertTrue(self.story_repository.delete(self.story.id))

        result = self.binder.bind_job(job_id)

        self.assertIsNone(result)
        self.assertIsNone(self.story_repository.get(self.story.id))

    def test_reapplying_the_same_job_is_not_a_duplicate(self) -> None:
        job_id = self._succeed_scene_job("scene_01", "visual")

        first = self.binder.bind_job(job_id)
        self.assertIsNotNone(first)
        scene = self.story_repository.get(self.story.id).scenes[0]
        self.assertEqual(scene.job_ids.count(job_id), 1)
        first_asset = scene.asset_ids["visual"]

        # A second live call for the same already-bound job (e.g. the event
        # firing twice) stays idempotent.
        self.binder.bind_job(job_id)
        scene = self.story_repository.get(self.story.id).scenes[0]
        self.assertEqual(scene.job_ids.count(job_id), 1)
        self.assertEqual(scene.asset_ids["visual"], first_asset)

        # A recovery replay of a job already recorded on the scene is a
        # deliberate no-op, not a second binding.
        replayed = self.binder.replay_job_safely(job_id)
        self.assertIsNone(replayed)
        scene = self.story_repository.get(self.story.id).scenes[0]
        self.assertEqual(scene.job_ids.count(job_id), 1)
        self.assertEqual(scene.asset_ids["visual"], first_asset)

    def test_recovery_replay_does_not_overwrite_a_newer_asset(self) -> None:
        # job1 succeeds but is never bound live -- as if the process crashed
        # before SceneBinder processed its completion event.
        job1_id = self._succeed_scene_job(
            "scene_01", "visual", output="outputs/images/a.png"
        )

        # job2 is a distinct, later generation for the same scene/role, bound
        # normally (this is the "current" state recovery must not clobber).
        job2_id = self._succeed_scene_job(
            "scene_01", "visual", output="outputs/images/b.png"
        )
        bound = self.binder.bind_job(job2_id)
        self.assertIsNotNone(bound)
        current_asset = self.story_repository.get(self.story.id).scenes[0].asset_ids[
            "visual"
        ]

        # Recovery now (for the first time) tries to replay the older job1.
        replayed = self.binder.replay_job_safely(job1_id)

        self.assertIsNone(replayed)
        scene = self.story_repository.get(self.story.id).scenes[0]
        self.assertEqual(scene.asset_ids["visual"], current_asset)
        self.assertNotIn(job1_id, scene.job_ids)
        self.assertIn(job2_id, scene.job_ids)

    def test_normal_completion_still_replaces_an_existing_role_asset(self) -> None:
        """Contrast with replay: an ordinary regeneration is allowed to win."""

        job1_id = self._succeed_scene_job(
            "scene_01", "visual", output="outputs/images/a.png"
        )
        self.binder.bind_job(job1_id)
        first_asset = self.story_repository.get(self.story.id).scenes[0].asset_ids[
            "visual"
        ]

        job2_id = self._succeed_scene_job(
            "scene_01", "visual", output="outputs/images/b.png"
        )
        bound = self.binder.bind_job(job2_id)  # live path, not a replay

        self.assertIsNotNone(bound)
        scene = self.story_repository.get(self.story.id).scenes[0]
        self.assertNotEqual(scene.asset_ids["visual"], first_asset)
        self.assertIn(job1_id, scene.job_ids)
        self.assertIn(job2_id, scene.job_ids)


@unittest.skipIf(IMPORT_ERROR is not None, f"missing dependency: {IMPORT_ERROR}")
class StoryApiConcurrencyTests(unittest.TestCase):
    """End-to-end proofs against the running API: requirements #1 and #4."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.services = create_application_services(
            db_path=root / "jobs.db", output_dir=root / "outputs" / "images"
        )
        self.client = TestClient(create_app(self.services, start_job_runner=False))

        self.story_id = self.client.post(
            "/stories", json={"title": "Old", "premise": "p"}
        ).json()["id"]
        story = self.services.story_repository.get(self.story_id)
        self.services.story_repository.save(
            apply_text_result(story, "scene_list", _SCENES)
        )

    def _run_concurrently(self, *targets) -> list[BaseException]:
        barrier = threading.Barrier(len(targets))
        errors: list[BaseException] = []

        def _wrap(target):
            def _call() -> None:
                try:
                    barrier.wait(timeout=_JOIN_TIMEOUT)
                    target()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            return _call

        threads = [threading.Thread(target=_wrap(target)) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT)
            self.assertFalse(thread.is_alive())
        return errors

    def test_api_patch_and_scene_binder_both_survive_concurrently(self) -> None:
        job = self.services.job_service.create_job(
            GenerationRequest(
                media_type="image",
                prompt="rooftop at dawn",
                model_id="sdxl",
                params=scene_binding_params(self.story_id, "scene_01", "visual"),
            )
        )

        def _patch() -> None:
            response = self.client.patch(
                f"/stories/{self.story_id}", json={"title": "New"}
            )
            self.assertEqual(response.status_code, 200, response.text)

        def _bind() -> None:
            # mark_succeeded publishes synchronously, which triggers
            # SceneBinder.handle_job_event -> bind_job on this thread -- the
            # same path a real job runner lane exercises in production.
            self.services.job_service.mark_succeeded(
                job.id,
                GenerationResult(
                    job_id=job.id,
                    status="succeeded",
                    outputs=["outputs/images/a.png"],
                    previews=["outputs/images/a.png"],
                    metadata={},
                ),
            )

        errors = self._run_concurrently(_patch, _bind)
        self.assertEqual(errors, [])

        story = self.services.story_repository.get(self.story_id)
        self.assertEqual(story.title, "New")
        self.assertIn("visual", story.scenes[0].asset_ids)

    def test_apply_text_result_and_api_patch_do_not_lose_independent_changes(
        self,
    ) -> None:
        job_id = self.client.post(
            f"/stories/{self.story_id}/expand",
            json={"task": "logline", "model_id": "template-writer", "params": {"count": 1}},
        ).json()["job_id"]
        processed = self.services.job_runner.run_once()
        self.assertIsNotNone(processed)
        self.assertEqual(
            self.services.job_repository.get(job_id).status, "succeeded"
        )

        def _patch() -> None:
            response = self.client.patch(
                f"/stories/{self.story_id}", json={"title": "New"}
            )
            self.assertEqual(response.status_code, 200, response.text)

        def _apply() -> None:
            response = self.client.post(
                f"/stories/{self.story_id}/apply", json={"job_id": job_id}
            )
            self.assertEqual(response.status_code, 200, response.text)

        errors = self._run_concurrently(_patch, _apply)
        self.assertEqual(errors, [])

        story = self.services.story_repository.get(self.story_id)
        self.assertEqual(story.title, "New")
        self.assertTrue(story.logline)
        self.assertEqual(story.source_job_ids, [job_id])


if __name__ == "__main__":
    unittest.main()
