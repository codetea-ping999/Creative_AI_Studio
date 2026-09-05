from contextlib import asynccontextmanager
import logging
import os
from threading import Event, Thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from bootstrap import ApplicationServices, create_application_services
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


def _release_after_worker_stops(worker: Thread, ownership: DataDirectoryOwnership) -> None:
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
    resolved_services = services or create_application_services()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = resolved_services
        stop_event: Event | None = None
        worker: Thread | None = None
        ownership = DataDirectoryOwnership(resolved_services.job_repository.data_directory)
        # Even with the runner disabled, startup sync and API writes require
        # exclusive authority. Classification does not activate any recovery.
        ownership.acquire()
        try:
            # Preserve the existing gallery-only startup synchronization.
            resolved_services.asset_repository.sync_jobs(
                resolved_services.job_repository.list()
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

            app.state.job_runner_stop_event = stop_event
            app.state.job_runner_thread = worker
            yield
        finally:
            if stop_event is not None:
                stop_event.set()
            if worker is not None and worker.ident is not None:
                worker.join(timeout=2.0)
            if worker is not None and worker.is_alive():
                # Lifespan exit is not proof that the daemon worker stopped.
                # A successor must not classify this live work as abandoned.
                logger.warning("Job worker is still stopping; retaining data-directory ownership.")
                Thread(
                    target=_release_after_worker_stops,
                    args=(worker, ownership),
                    daemon=True,
                    name="creative-ai-ownership-release",
                ).start()
            else:
                ownership.release()

    app = FastAPI(title="Creative AI Studio API", lifespan=lifespan)
    app.state.services = resolved_services
    app.state.agent_protocol = AgentProtocol()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_local_web_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    output_root = resolved_services.output_dir.parent
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
