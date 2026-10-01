BEGIN TRANSACTION;
CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    progress REAL NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
INSERT INTO "jobs" VALUES('job_fx_succeeded',NULL,'image','succeeded','{"media_type": "image", "model_id": "fake", "negative_prompt": null, "output_format": null, "params": {"scene_id": "scene_1", "scene_role": "visual", "story_id": "story_fixture"}, "prompt": "a lighthouse at dawn", "references": [], "seed": null, "task_type": null}','{"error_message": null, "job_id": "job_fx_succeeded", "metadata": {"seed": 7}, "outputs": ["outputs/images/job_fx_succeeded.png"], "previews": [], "status": "succeeded"}',1.0,NULL,'2026-09-01T09:00:00+00:00','2026-09-01T09:01:00+00:00');
INSERT INTO "jobs" VALUES('job_fx_running',NULL,'image','running','{"media_type": "image", "model_id": "fake", "negative_prompt": null, "output_format": null, "params": {"scene_id": "scene_2", "scene_role": "visual", "story_id": "story_fixture"}, "prompt": "a harbor at noon", "references": [], "seed": null, "task_type": null}',NULL,0.4,NULL,'2026-09-01T09:02:00+00:00','2026-09-01T09:03:00+00:00');
INSERT INTO "jobs" VALUES('job_fx_queued',NULL,'image','queued','{"media_type": "image", "model_id": "fake", "negative_prompt": null, "output_format": null, "params": {"scene_id": "scene_3", "scene_role": "visual", "story_id": "story_fixture"}, "prompt": "a storm at night", "references": [], "seed": null, "task_type": null}',NULL,0.0,NULL,'2026-09-01T09:04:00+00:00','2026-09-01T09:04:00+00:00');
COMMIT;
