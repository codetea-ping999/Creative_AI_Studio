"""Default dependency composition for generators, jobs, and API services."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from core.assets import AssetRepository
from core.batches import BatchRepository, BatchService
from core.bible import BibleRepository
from core.jobs import CancellationRegistry, EventBus, JobQueue, JobRunner, JobService
from core.jobs.completion import CompletionConverger
from core.models import (
    ModelRuntimeCache,
    ModelRegistry,
    ModelResolver,
    ModelService,
    create_default_loader_registry,
    release_runtime,
    resolve_media_cache_limits,
)
from core.feedback import FeedbackRepository
from core.projects import ProjectRepository
from core.prompting import PromptComposer
from core.story import SceneBinder, StoryRepository
from core.storage.repositories.job_repository import JobRepository
from generators.audio import AudioGenerator, SpeechGenerator
from generators.image import ImageGenerator
from generators.registry import GeneratorRegistry
from generators.text import TextGenerator
from generators.video import AssemblyGenerator, VideoGenerator


@dataclass(slots=True)
class ApplicationServices:
    """Shared app-level service container for the API process."""

    output_dir: Path
    model_service: ModelService
    generator_registry: GeneratorRegistry
    job_repository: JobRepository
    job_queue: JobQueue
    event_bus: EventBus
    job_service: JobService
    job_runner: JobRunner
    project_repository: ProjectRepository
    feedback_repository: FeedbackRepository
    asset_repository: AssetRepository
    bible_repository: BibleRepository
    prompt_composer: PromptComposer
    story_repository: StoryRepository
    scene_binder: SceneBinder
    batch_repository: BatchRepository
    batch_service: BatchService
    completion_converger: CompletionConverger


def _resolve_manifest_root(manifest_root: str | Path | None) -> str | Path | None:
    if manifest_root is not None:
        return manifest_root

    manifest_root_env = os.getenv("MODELS_MANIFEST_ROOT")
    if manifest_root_env:
        return manifest_root_env

    models_root_env = os.getenv("MODELS_ROOT")
    if models_root_env:
        return Path(models_root_env) / "manifests"

    return None


def _resolve_output_dir(output_dir: str | Path | None) -> Path:
    if output_dir is not None:
        return Path(output_dir)

    output_image_dir_env = os.getenv("OUTPUT_IMAGE_DIR")
    if output_image_dir_env:
        return Path(output_image_dir_env)

    output_root_env = os.getenv("OUTPUT_DIR")
    if output_root_env:
        return Path(output_root_env) / "images"

    return Path("outputs/images")


def _resolve_audio_output_dir(output_dir: str | Path | None) -> Path:
    if output_dir is not None:
        return Path(output_dir).parent / "audio"

    output_audio_dir_env = os.getenv("OUTPUT_AUDIO_DIR")
    if output_audio_dir_env:
        return Path(output_audio_dir_env)

    resolved_image_output_dir = _resolve_output_dir(output_dir)
    return resolved_image_output_dir.parent / "audio"


def _resolve_text_output_dir(output_dir: str | Path | None) -> Path:
    if output_dir is not None:
        return Path(output_dir).parent / "text"

    output_text_dir_env = os.getenv("OUTPUT_TEXT_DIR")
    if output_text_dir_env:
        return Path(output_text_dir_env)

    return _resolve_output_dir(output_dir).parent / "text"


def _resolve_db_path(db_path: str | Path | None) -> Path:
    if db_path is not None:
        return Path(db_path)

    return Path(os.getenv("DB_PATH", "data/jobs.db"))


def _resolve_max_cached_models(max_cached_models: int | None) -> int:
    if max_cached_models is not None:
        return max_cached_models

    raw_value = os.getenv("MAX_CACHED_MODELS", "1")
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 1


def create_default_model_service(
    manifest_root: str | Path | None = None,
    *,
    max_cached_models: int | None = None,
) -> ModelService:
    """Construct the default model service used by local generation flows."""

    resolved_manifest_root = _resolve_manifest_root(manifest_root)
    resolved_max_cached_models = _resolve_max_cached_models(max_cached_models)

    registry = ModelRegistry(manifest_root=resolved_manifest_root)
    resolver = ModelResolver(registry)
    loader_registry = create_default_loader_registry()
    runtime_cache = ModelRuntimeCache(
        max_entries=resolved_max_cached_models,
        media_limits=resolve_media_cache_limits(),
        on_evict=release_runtime,
    )
    return ModelService(
        registry=registry,
        resolver=resolver,
        loader_registry=loader_registry,
        runtime_cache=runtime_cache,
    )


def create_default_image_generator(
    output_dir: str | Path | None = None,
    *,
    model_service: ModelService | None = None,
    manifest_root: str | Path | None = None,
    max_cached_models: int | None = None,
    task_type: str = "text-to-image",
    prompt_composer: PromptComposer | None = None,
) -> ImageGenerator:
    """Compose the default image stub generator with its model service."""

    resolved_output_dir = _resolve_output_dir(output_dir)
    resolved_model_service = model_service or create_default_model_service(
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
    )
    return ImageGenerator(
        resolved_model_service,
        output_dir=resolved_output_dir,
        task_type=task_type,
        prompt_composer=prompt_composer,
    )


def create_default_text_generator(
    output_dir: str | Path | None = None,
    *,
    model_service: ModelService | None = None,
    manifest_root: str | Path | None = None,
    max_cached_models: int | None = None,
    task_type: str = "story",
) -> TextGenerator:
    """Compose the default local text generator."""

    resolved_model_service = model_service or create_default_model_service(
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
    )
    return TextGenerator(
        resolved_model_service,
        output_dir=_resolve_text_output_dir(output_dir),
        task_type=task_type,
    )


def create_default_audio_generator(
    output_dir: str | Path | None = None,
    *,
    model_service: ModelService | None = None,
    asset_repository: AssetRepository | None = None,
    manifest_root: str | Path | None = None,
    max_cached_models: int | None = None,
    task_type: str = "text-to-music",
) -> AudioGenerator:
    """Compose the default audio generator."""

    resolved_model_service = model_service or create_default_model_service(
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
    )
    return AudioGenerator(
        resolved_model_service,
        output_dir=_resolve_audio_output_dir(output_dir),
        asset_repository=asset_repository,
        task_type=task_type,
    )


def create_default_speech_generator(
    output_dir: str | Path | None = None,
    *,
    model_service: ModelService | None = None,
    manifest_root: str | Path | None = None,
    max_cached_models: int | None = None,
) -> SpeechGenerator:
    """Compose the text-to-speech narration generator."""

    resolved_model_service = model_service or create_default_model_service(
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
    )
    return SpeechGenerator(
        resolved_model_service,
        output_dir=_resolve_audio_output_dir(output_dir),
    )


def create_default_video_generator(
    output_dir: str | Path | None = None,
    *,
    model_service: ModelService | None = None,
    manifest_root: str | Path | None = None,
    max_cached_models: int | None = None,
    task_type: str = "text-to-video",
) -> VideoGenerator:
    """Compose the default local storyboard video generator."""

    resolved_model_service = model_service or create_default_model_service(
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
    )
    resolved_output_dir = _resolve_output_dir(output_dir).parent / "videos"
    return VideoGenerator(
        resolved_model_service,
        output_dir=resolved_output_dir,
        task_type=task_type,
    )


def create_default_assembly_generator(
    output_dir: str | Path | None = None,
    *,
    asset_repository: AssetRepository | None = None,
) -> AssemblyGenerator:
    """Compose the deterministic timeline assembly generator."""

    def lookup_asset_path(asset_id: str) -> str | None:
        if asset_repository is None:
            return None
        asset = asset_repository.get(asset_id)
        return asset.path if asset is not None else None

    return AssemblyGenerator(
        output_dir=_resolve_output_dir(output_dir).parent / "videos",
        asset_path_lookup=lookup_asset_path,
        allow_direct_paths=False,
    )


def create_default_generator_registry(
    *,
    model_service: ModelService | None = None,
    asset_repository: AssetRepository | None = None,
    manifest_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    max_cached_models: int | None = None,
    prompt_composer: PromptComposer | None = None,
) -> GeneratorRegistry:
    """Compose the generator registry for every supported media type."""

    resolved_output_dir = _resolve_output_dir(output_dir)
    resolved_model_service = model_service or create_default_model_service(
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
    )
    registry = GeneratorRegistry()
    image_generator = create_default_image_generator(
        output_dir=resolved_output_dir,
        model_service=resolved_model_service,
        task_type="text-to-image",
        prompt_composer=prompt_composer,
    )
    registry.register("image", image_generator)
    registry.register("image", image_generator, task_type="text-to-image")

    text_generator = create_default_text_generator(
        output_dir=resolved_output_dir,
        model_service=resolved_model_service,
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
        task_type="story",
    )
    registry.register("text", text_generator)
    registry.register("text", text_generator, task_type="story")

    audio_generator = create_default_audio_generator(
        output_dir=resolved_output_dir,
        model_service=resolved_model_service,
        # Melody conditioning resolves a gallery asset as its reference, so the
        # audio generator needs the asset repository.
        asset_repository=asset_repository,
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
        task_type="text-to-music",
    )
    registry.register("audio", audio_generator)
    registry.register("audio", audio_generator, task_type="text-to-music")
    registry.register(
        "audio",
        create_default_speech_generator(
            output_dir=resolved_output_dir,
            model_service=resolved_model_service,
            manifest_root=manifest_root,
            max_cached_models=max_cached_models,
        ),
        task_type="text-to-speech",
    )
    video_generator = create_default_video_generator(
        output_dir=resolved_output_dir,
        model_service=resolved_model_service,
        manifest_root=manifest_root,
        max_cached_models=max_cached_models,
        task_type="text-to-video",
    )
    registry.register("video", video_generator)
    registry.register("video", video_generator, task_type="text-to-video")
    registry.register(
        "video",
        create_default_assembly_generator(
            output_dir=resolved_output_dir,
            asset_repository=asset_repository,
        ),
        task_type="assembly",
    )
    return registry


def create_application_services(
    *,
    manifest_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    max_cached_models: int | None = None,
) -> ApplicationServices:
    """Create the shared single-process service graph used by the API."""

    resolved_manifest_root = _resolve_manifest_root(manifest_root)
    resolved_output_dir = _resolve_output_dir(output_dir)
    resolved_db_path = _resolve_db_path(db_path)
    resolved_max_cached_models = _resolve_max_cached_models(max_cached_models)

    model_service = create_default_model_service(
        manifest_root=resolved_manifest_root,
        max_cached_models=resolved_max_cached_models,
    )
    bible_repository = BibleRepository(resolved_db_path.parent / "bible")
    job_repository = JobRepository(resolved_db_path)
    asset_repository = AssetRepository(resolved_db_path.parent / "assets")
    # asset_repository must exist before the composer so bible-derived
    # character/location references can be checked against the real asset
    # store (#199) -- without it, MissingReferenceAssetError never fires in
    # the running app even though it's fully implemented and tested.
    prompt_composer = PromptComposer(bible_repository, asset_repository)
    generator_registry = create_default_generator_registry(
        model_service=model_service,
        asset_repository=asset_repository,
        manifest_root=resolved_manifest_root,
        output_dir=resolved_output_dir,
        max_cached_models=resolved_max_cached_models,
        prompt_composer=prompt_composer,
    )
    job_queue = JobQueue()
    event_bus = EventBus()
    cancellation_registry = CancellationRegistry()
    job_service = JobService(
        job_repository,
        job_queue,
        event_bus,
        asset_repository=asset_repository,
        cancellation_registry=cancellation_registry,
        model_service=model_service,
        bible_repository=bible_repository,
    )
    job_runner = JobRunner(
        job_repository,
        job_queue,
        generator_registry,
        event_bus,
        asset_repository=asset_repository,
        job_service=job_service,
        cancellation_registry=cancellation_registry,
    )
    project_repository = ProjectRepository(resolved_db_path.parent / "projects")
    feedback_repository = FeedbackRepository(resolved_db_path.parent / "feedback")
    story_repository = StoryRepository(resolved_db_path.parent / "stories")
    batch_repository = BatchRepository(resolved_db_path.parent / "batches")
    batch_service = BatchService(
        batch_repository,
        job_service,
        job_repository,
        event_bus=event_bus,
    )
    # Subscribing here means a probe stage advances to refine on its own as soon
    # as its last child finishes, without the UI having to poll and push.
    batch_service.attach_to_event_bus()
    scene_binder = SceneBinder(
        story_repository,
        job_repository,
        asset_repository,
        event_bus=event_bus,
    )
    # Subscribing means a scene picks up its image or narration the moment that
    # job finishes, no matter who started it.
    scene_binder.attach_to_event_bus()
    completion_converger = CompletionConverger(
        job_repository,
        asset_repository,
        story_repository=story_repository,
        scene_binder=scene_binder,
        batch_service=batch_service,
    )
    # Subscribing means "succeeded Job != completion fully applied" gets
    # resolved live, the moment a job finishes, via the exact same
    # convergence path startup recovery and a runtime retry pass also use --
    # not a second, divergent live-only implementation.
    completion_converger.attach_to_event_bus(event_bus)
    return ApplicationServices(
        output_dir=resolved_output_dir,
        model_service=model_service,
        generator_registry=generator_registry,
        job_repository=job_repository,
        job_queue=job_queue,
        event_bus=event_bus,
        job_service=job_service,
        job_runner=job_runner,
        project_repository=project_repository,
        feedback_repository=feedback_repository,
        asset_repository=asset_repository,
        bible_repository=bible_repository,
        prompt_composer=prompt_composer,
        story_repository=story_repository,
        scene_binder=scene_binder,
        batch_repository=batch_repository,
        batch_service=batch_service,
        completion_converger=completion_converger,
    )

__all__ = [
    "ApplicationServices",
    "create_application_services",
    "create_default_assembly_generator",
    "create_default_audio_generator",
    "create_default_generator_registry",
    "create_default_image_generator",
    "create_default_model_service",
    "create_default_speech_generator",
    "create_default_text_generator",
    "create_default_video_generator",
]
