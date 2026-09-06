from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from bootstrap import ApplicationServices, create_application_services
from core.jobs.startup_recovery import run_startup_recovery
from apps.api.routes.agent import router as agent_router
from apps.api.routes.batches import router as batches_router
from apps.api.routes.bible import router as bible_router
from apps.api.routes.catalog import router as catalog_router
from apps.api.routes.feedback import router as feedback_router
from apps.api.routes.gallery import router as gallery_router
from apps.api.routes.generate import router as generate_router
from apps.api.routes.health import router as health_router
from apps.api.routes.jobs import router as jobs_router
from apps.api.routes.metrics import router as metrics_router
from apps.api.routes.models import router as models_router
from apps.api.routes.projects import router as projects_router
from apps.api.routes.stories import router as stories_router
from core.remote import AgentProtocol
from core.storage.ownership import DataDirectoryOwnership

logger = logging.getLogger(__name__)


def _configured_db_path() -> Path:
    """Resolve the configured SQLite path without constructing repositories."""

    return Path(os.getenv("DB_PATH", "data/jobs.db"))


def _configured_output_dir() -> Path:
    """Resolve the configured image output directory without service setup."""

    output_image_dir = os.getenv("OUTPUT_IMAGE_DIR")
    if output_image_dir:
        return Path(output_image_dir)
    output_root = os.getenv("OUTPUT_DIR")
    if output_root:
        return Path(output_root) / "images"
    return Path("outputs/images")


def _release_after_workers_stop(
    workers: list[Thread], ownership: DataDirectoryOwnership
) -> None:
    for worker in workers:
        worker.join()
    ownership.release()


def _local_web_origins() -> list[str]:
    """Return the loopback dev origins configured for the local Vite server."""

    configured_port = os.getenv("WEB_PORT", "5173")
    try:
        web_port = int(configured_port)
    except ValueError:
        web_port = 5173
    if not 1 <= web_port <= 65535:
        web_port = 5173
    return [
        f"http://127.0.0.1:{web_port}",
        f"http://localhost:{web_port}",
    ]


def create_app(
    services: ApplicationServices | None = None,
    *,
    start_job_runner: bool = True,
) -> FastAPI:
    # Keep the default service graph lazy.  JobRepository creates and migrates
    # SQLite during construction, so it must not run until this process owns
    # the configured data directory.
    resolved_services = services
    data_directory = (
        resolved_services.job_repository.data_directory
        if resolved_services is not None
        else _configured_db_path().resolve().parent
    )
    output_dir = (
        resolved_services.output_dir
        if resolved_services is not None
        else _configured_output_dir()
    )
    ownership = DataDirectoryOwnership(data_directory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal resolved_services
        stop_event: Event | None = None
        worker: Thread | None = None
        retry_stop_event: Event | None = None
        retry_thread: Thread | None = None
        # Even with the runner disabled, startup sync and API writes require
        # exclusive authority. Classification does not activate any recovery.
        ownership.acquire()
        try:
            if resolved_services is None:
                resolved_services = create_application_services()
            app.state.services = resolved_services

            # PR3: startup recovery -- poison-row isolation, interrupted/
            # cancel_requested convergence, completion convergence, and
            # queued-job status re-check all happen here, synchronously,
            # strictly before the job runner thread (below) can dequeue
            # anything. This supersedes the old gallery-only
            # asset_repository.sync_jobs() call: completion convergence
            # already syncs every terminal-and-pending job's Asset (a
            # strict superset), Story-replays it, and reconciles its Batch.
            recovery_report = run_startup_recovery(
                resolved_services.job_repository,
                resolved_services.job_service,
                resolved_services.completion_converger,
                batch_service=resolved_services.batch_service,
            )
            if recovery_report.poison_rows or recovery_report.interrupted_failed or recovery_report.cancel_requested_cancelled:
                logger.info(
                    "Startup recovery: %d poison row(s), %d interrupted job(s) "
                    "failed, %d cancel_requested job(s) cancelled, %d job(s) "
                    "re-enqueued.",
                    len(recovery_report.poison_rows),
                    len(recovery_report.interrupted_failed),
                    len(recovery_report.cancel_requested_cancelled),
                    len(recovery_report.requeued),
                )

            if start_job_runner:
                stop_event = Event()
                worker = Thread(
                    target=resolved_services.job_runner.run_forever,
                    kwargs={"stop_event": stop_event},
                    daemon=True,
                    name="creative-ai-job-runner",
                )
                worker.start()

                # PR3: a minimal single-thread runtime retry loop for
                # completion convergence -- not a WorkerPool, not a new
                # lane. Joined below before ownership is ever released, the
                # same as the job runner thread.
                retry_stop_event = Event()
                retry_thread = Thread(
                    target=resolved_services.completion_converger.run_retry_loop,
                    kwargs={"stop_event": retry_stop_event},
                    daemon=True,
                    name="creative-ai-completion-retry",
                )
                retry_thread.start()

            app.state.job_runner_stop_event = stop_event
            app.state.job_runner_thread = worker
            app.state.completion_retry_stop_event = retry_stop_event
            app.state.completion_retry_thread = retry_thread
            yield
        finally:
            if retry_stop_event is not None:
                retry_stop_event.set()
            if stop_event is not None:
                stop_event.set()
            if retry_thread is not None and retry_thread.ident is not None:
                retry_thread.join(timeout=2.0)
            if worker is not None and worker.ident is not None:
                worker.join(timeout=2.0)
            still_running = (worker is not None and worker.is_alive()) or (
                retry_thread is not None and retry_thread.is_alive()
            )
            if still_running:
                # Lifespan exit is not proof either background thread
                # stopped. A successor must not classify this live work as
                # abandoned, so ownership is only released once both are
                # confirmed joined.
                logger.warning(
                    "Job worker and/or completion retry loop still stopping; "
                    "retaining data-directory ownership."
                )
                Thread(
                    target=_release_after_workers_stop,
                    args=([t for t in (worker, retry_thread) if t is not None], ownership),
                    daemon=True,
                    name="creative-ai-ownership-release",
                ).start()
            else:
                ownership.release()

    app = FastAPI(title="Creative AI Studio API", lifespan=lifespan)
    if resolved_services is not None:
        app.state.services = resolved_services
    app.state.agent_protocol = AgentProtocol()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_local_web_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    output_root = output_dir.parent
    output_root.mkdir(parents=True, exist_ok=True)
    app.mount("/outputs", StaticFiles(directory=output_root), name="outputs")

    app.include_router(health_router)
    app.include_router(agent_router)
    app.include_router(jobs_router)
    app.include_router(projects_router)
    app.include_router(models_router)
    app.include_router(metrics_router)
    app.include_router(catalog_router)
    app.include_router(gallery_router)
    app.include_router(feedback_router)
    app.include_router(generate_router)
    app.include_router(bible_router)
    app.include_router(batches_router)
    app.include_router(stories_router)

    return app


app = create_app()
