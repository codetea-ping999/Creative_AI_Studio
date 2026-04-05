import { startTransition, useEffect, useState } from "react";
import {
  PromptForm,
  type LoraOption,
  type MediaType,
  type ModelOption,
  type PromptFormSubmitValues,
} from "./components/PromptForm";

type JobStatus =
  | "queued"
  | "preparing"
  | "running"
  | "postprocessing"
  | "succeeded"
  | "failed"
  | "cancelled";

type ThemeMode = "light" | "dark";

type CreateJobResponse = {
  job_id: string;
  status: JobStatus;
};

type JobResponse = {
  id: string;
  media_type: MediaType;
  project_id: string | null;
  status: JobStatus;
  progress: number;
  error_message: string | null;
  request: GenerationRequestSnapshot;
  result: {
    outputs: string[];
    previews: string[];
    metadata: Record<string, unknown>;
  } | null;
  created_at: string;
  updated_at: string;
};

type GenerationRequestSnapshot = {
  media_type: MediaType;
  prompt: string;
  negative_prompt: string | null;
  model_id: string;
  seed: number | null;
  output_format: string | null;
  params: Record<string, unknown>;
};

type ModelSummary = {
  id: string;
  display_name: string;
  default_params: Record<string, unknown>;
  tags: string[];
  is_available: boolean;
  is_default: boolean;
};

type ModelsResponse = {
  models: ModelSummary[];
};

type LoraCatalogResponse = {
  items: Array<{
    id: string;
    display_name: string;
    path: string;
    relative_path: string;
  }>;
};

type GalleryItemResponse = {
  asset_id: string;
  job_id: string;
  project_id: string | null;
  project_name: string | null;
  media_type: MediaType;
  prompt: string;
  model_id: string;
  output_path: string;
  preview_path: string | null;
  created_at: string;
  updated_at: string;
  quality_score: number | null;
  quality_level: string | null;
  semantic_alignment_score: number | null;
  creative_alignment_score: number | null;
  quality_score_calibrated: number | null;
  semantic_alignment_score_calibrated: number | null;
  creative_alignment_score_calibrated: number | null;
  feedback_count: number;
  average_feedback_quality: number | null;
  reuse_count: number;
  export_count: number;
  success: boolean;
};

type GalleryAssetDetailResponse = GalleryItemResponse & {
  quality_report: Record<string, unknown>;
  request_snapshot: GenerationRequestSnapshot;
  metadata: Record<string, unknown>;
  feedback_summary: Record<string, unknown>;
  export_paths: string[];
  parent_asset_id: string | null;
  lineage: string[];
  tags: string[];
};

type GalleryStatsResponse = {
  total_items: number;
  total_by_media_type: Record<string, number>;
  total_by_project: Record<string, number>;
  average_quality_score: number | null;
  total_reuse_count: number;
  total_export_count: number;
};

type ReuseAssetResponse = {
  asset_id: string;
  job_id: string;
  status: JobStatus;
  project_id: string | null;
};

type ExportAssetResponse = {
  asset_id: string;
  export_path: string;
  metadata_path: string | null;
};

type MetricsSummaryResponse = {
  total_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  running_jobs: number;
  success_rate: number;
  average_quality_score: number | null;
  average_semantic_alignment_score: number | null;
  average_creative_alignment_score: number | null;
  feedback_total: number;
  feedback_coverage_rate: number;
  by_media: Partial<Record<MediaType, MediaMetrics>>;
};

type MediaMetrics = {
  total_jobs: number;
  success_rate: number;
  average_quality_score: number | null;
  average_semantic_alignment_score: number | null;
  average_creative_alignment_score: number | null;
  feedback_total: number;
  feedback_coverage_rate: number;
};

type ProjectResponse = {
  id: string;
  name: string;
  description: string;
  status: string;
  asset_count: number;
  job_count: number;
};

type RefreshStudioOptions = {
  preferredAssetId?: string | null;
  preferredJobId?: string | null;
};

const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";

const terminalStatuses = new Set<JobStatus>(["succeeded", "failed", "cancelled"]);

const defaultSubmitValues: Record<MediaType, PromptFormSubmitValues> = {
  image: {
    mediaType: "image",
    modelId: "sdxl",
    prompt: "",
    negativePrompt: "",
    width: 1024,
    height: 1024,
    steps: 30,
    guidanceScale: 7.5,
    loraPath: "",
    loraScale: 0.8,
    seed: null,
    durationSeconds: 8,
    bpm: 96,
    mood: "dreamy",
    cameraMotion: "push-in",
    visualStyle: "storyboard",
  },
  audio: {
    mediaType: "audio",
    modelId: "musicgen-small",
    prompt: "",
    negativePrompt: "",
    width: 1024,
    height: 1024,
    steps: 30,
    guidanceScale: 3,
    loraPath: "",
    loraScale: 0.8,
    seed: null,
    durationSeconds: 8,
    bpm: 96,
    mood: "dreamy",
    cameraMotion: "push-in",
    visualStyle: "storyboard",
  },
  video: {
    mediaType: "video",
    modelId: "storyboard-video",
    prompt: "",
    negativePrompt: "",
    width: 576,
    height: 320,
    steps: 30,
    guidanceScale: 7.5,
    loraPath: "",
    loraScale: 0.8,
    seed: null,
    durationSeconds: 4,
    bpm: 96,
    mood: "dreamy",
    cameraMotion: "push-in",
    visualStyle: "storyboard",
  },
};

const mediaTypeLabels: Record<MediaType, string> = {
  image: "Image",
  audio: "Audio",
  video: "Video",
};

function normalizeModelOption(item: ModelSummary): ModelOption {
  return {
    id: item.id,
    displayName: item.display_name,
    defaultParams: item.default_params,
    tags: item.tags,
    isAvailable: item.is_available,
    isDefault: item.is_default,
  };
}

function normalizeLoraOption(item: LoraCatalogResponse["items"][number]): LoraOption {
  return {
    id: item.id,
    displayName: item.display_name,
    path: item.path,
    relativePath: item.relative_path,
  };
}

function createOutputUrl(pathValue: string | null | undefined): string | null {
  if (!pathValue) {
    return null;
  }

  const normalized = pathValue.replace(/\\/g, "/");
  const marker = "/outputs/";
  const markerIndex = normalized.lastIndexOf(marker);
  if (markerIndex < 0) {
    return null;
  }

  return `${API_BASE_URL}${normalized.slice(markerIndex)}`;
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "n/a";
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) {
    return value;
  }

  return parsed.toLocaleString();
}

function formatScore(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(1) : "n/a";
}

function formatPercent(value: number | null | undefined): string {
  return typeof value === "number" ? `${value.toFixed(1)}%` : "n/a";
}

function isAudioAsset(pathValue: string | null | undefined): boolean {
  return Boolean(pathValue && /\.(wav|mp3|ogg|m4a)$/i.test(pathValue));
}

function isVideoAsset(pathValue: string | null | undefined): boolean {
  return Boolean(pathValue && /\.(mp4|webm|mov)$/i.test(pathValue));
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function extractQualityReport(
  metadata: Record<string, unknown> | null | undefined,
): Record<string, unknown> | null {
  if (!metadata) {
    return null;
  }

  const qualityReport = metadata.quality_report;
  return qualityReport && typeof qualityReport === "object"
    ? (qualityReport as Record<string, unknown>)
    : null;
}

function extractJobQualityScore(job: JobResponse | null): number | null {
  return asNumber(extractQualityReport(job?.result?.metadata)?.quality_score);
}

function mergeDraftWithDefaults(
  mediaType: MediaType,
  draft?: Partial<PromptFormSubmitValues>,
): PromptFormSubmitValues {
  return {
    ...defaultSubmitValues[mediaType],
    ...draft,
    mediaType,
  };
}

function createDraftFromRequestSnapshot(
  request: GenerationRequestSnapshot,
): Partial<PromptFormSubmitValues> {
  const params = request.params ?? {};
  if (request.media_type === "image") {
    return {
      mediaType: "image",
      modelId: request.model_id,
      prompt: request.prompt,
      negativePrompt: request.negative_prompt ?? "",
      width: asNumber(params.width) ?? defaultSubmitValues.image.width,
      height: asNumber(params.height) ?? defaultSubmitValues.image.height,
      steps: asNumber(params.steps) ?? defaultSubmitValues.image.steps,
      guidanceScale:
        asNumber(params.guidance_scale) ?? defaultSubmitValues.image.guidanceScale,
      loraPath: asString(params.lora_path) ?? "",
      loraScale: asNumber(params.lora_scale) ?? defaultSubmitValues.image.loraScale,
      seed: request.seed,
    };
  }

  if (request.media_type === "audio") {
    return {
      mediaType: "audio",
      modelId: request.model_id,
      prompt: request.prompt,
      durationSeconds:
        asNumber(params.duration_seconds) ?? defaultSubmitValues.audio.durationSeconds,
      guidanceScale:
        asNumber(params.guidance_scale) ?? defaultSubmitValues.audio.guidanceScale,
      bpm: asNumber(params.bpm) ?? defaultSubmitValues.audio.bpm,
      mood: asString(params.mood) ?? defaultSubmitValues.audio.mood,
      seed: request.seed,
    };
  }

  return {
    mediaType: "video",
    modelId: request.model_id,
    prompt: request.prompt,
    negativePrompt: request.negative_prompt ?? "",
    width: asNumber(params.width) ?? defaultSubmitValues.video.width,
    height: asNumber(params.height) ?? defaultSubmitValues.video.height,
    durationSeconds:
      asNumber(params.duration_seconds) ?? defaultSubmitValues.video.durationSeconds,
    cameraMotion:
      asString(params.camera_motion) ?? defaultSubmitValues.video.cameraMotion,
    visualStyle:
      asString(params.visual_style) ?? defaultSubmitValues.video.visualStyle,
    seed: request.seed,
  };
}

function buildGeneratePayload(
  values: PromptFormSubmitValues,
  projectId: string | null,
): Record<string, unknown> {
  if (values.mediaType === "image") {
    return {
      prompt: values.prompt,
      negative_prompt: values.negativePrompt || null,
      model_id: values.modelId,
      seed: values.seed,
      project_id: projectId,
      output_format: "png",
      params: {
        width: values.width,
        height: values.height,
        steps: values.steps,
        guidance_scale: values.guidanceScale,
        ...(values.loraPath ? { lora_path: values.loraPath, lora_scale: values.loraScale } : {}),
      },
    };
  }

  if (values.mediaType === "audio") {
    return {
      prompt: values.prompt,
      model_id: values.modelId,
      seed: values.seed,
      project_id: projectId,
      output_format: "wav",
      params: {
        duration_seconds: values.durationSeconds,
        guidance_scale: values.guidanceScale,
        bpm: values.bpm,
        mood: values.mood,
      },
    };
  }

  return {
    prompt: values.prompt,
    negative_prompt: values.negativePrompt || null,
    model_id: values.modelId,
    seed: values.seed,
    project_id: projectId,
    output_format: "gif",
    params: {
      width: values.width,
      height: values.height,
      duration_seconds: values.durationSeconds,
      camera_motion: values.cameraMotion,
      visual_style: values.visualStyle,
    },
  };
}

function buildReusePayload(
  values: PromptFormSubmitValues,
  projectId: string | null,
): Record<string, unknown> {
  if (values.mediaType === "image") {
    return {
      action: "variation",
      prompt: values.prompt,
      negative_prompt: values.negativePrompt || null,
      model_id: values.modelId,
      seed: values.seed,
      output_format: "png",
      project_id: projectId,
      params: {
        width: values.width,
        height: values.height,
        steps: values.steps,
        guidance_scale: values.guidanceScale,
        ...(values.loraPath ? { lora_path: values.loraPath, lora_scale: values.loraScale } : {}),
      },
    };
  }

  if (values.mediaType === "audio") {
    return {
      action: "variation",
      prompt: values.prompt,
      model_id: values.modelId,
      seed: values.seed,
      output_format: "wav",
      project_id: projectId,
      params: {
        duration_seconds: values.durationSeconds,
        guidance_scale: values.guidanceScale,
        bpm: values.bpm,
        mood: values.mood,
      },
    };
  }

  return {
    action: "variation",
    prompt: values.prompt,
    negative_prompt: values.negativePrompt || null,
    model_id: values.modelId,
    seed: values.seed,
    output_format: "gif",
    project_id: projectId,
    params: {
      width: values.width,
      height: values.height,
      duration_seconds: values.durationSeconds,
      camera_motion: values.cameraMotion,
      visual_style: values.visualStyle,
    },
  };
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

function App() {
  const [themeMode, setThemeMode] = useState<ThemeMode>("light");
  const [mediaType, setMediaType] = useState<MediaType>("image");
  const [composerRevision, setComposerRevision] = useState(0);
  const [modelOptionsByMedia, setModelOptionsByMedia] = useState<
    Record<MediaType, ModelOption[]>
  >({
    image: [],
    audio: [],
    video: [],
  });
  const [loraOptions, setLoraOptions] = useState<LoraOption[]>([]);
  const [drafts, setDrafts] = useState<Record<MediaType, Partial<PromptFormSubmitValues>>>({
    image: defaultSubmitValues.image,
    audio: defaultSubmitValues.audio,
    video: defaultSubmitValues.video,
  });
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [galleryItems, setGalleryItems] = useState<GalleryItemResponse[]>([]);
  const [galleryStats, setGalleryStats] = useState<GalleryStatsResponse | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedAssetDetail, setSelectedAssetDetail] =
    useState<GalleryAssetDetailResponse | null>(null);
  const [selectedAssetProjectId, setSelectedAssetProjectId] = useState("");
  const [metrics, setMetrics] = useState<MetricsSummaryResponse | null>(null);
  const [latestJob, setLatestJob] = useState<JobResponse | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isAssetBusy, setIsAssetBusy] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [assetMessage, setAssetMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const activeModels = modelOptionsByMedia[mediaType];
  const activeMetrics = metrics?.by_media[mediaType] ?? null;

  useEffect(() => {
    document.documentElement.dataset.theme = themeMode;
  }, [themeMode]);

  useEffect(() => {
    void loadProjects();
    void loadLoras();
  }, []);

  useEffect(() => {
    void loadModels(mediaType);
    void refreshStudio(mediaType);
  }, [mediaType]);

  useEffect(() => {
    if (!activeJobId) {
      return undefined;
    }

    const timer = window.setInterval(() => {
      void loadJob(activeJobId, true);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeJobId]);

  async function loadProjects(): Promise<void> {
    try {
      const payload = await requestJson<ProjectResponse[]>("/projects");
      startTransition(() => {
        setProjects(payload);
      });
    } catch (error) {
      console.error(error);
    }
  }

  async function loadLoras(): Promise<void> {
    try {
      const payload = await requestJson<LoraCatalogResponse>("/catalog/loras");
      startTransition(() => {
        setLoraOptions(payload.items.map(normalizeLoraOption));
      });
    } catch (error) {
      console.error(error);
    }
  }

  async function loadModels(targetMediaType: MediaType): Promise<void> {
    try {
      const payload = await requestJson<ModelsResponse>(
        `/models?media_type=${encodeURIComponent(targetMediaType)}`,
      );
      const nextModels = payload.models.map(normalizeModelOption);
      startTransition(() => {
        setModelOptionsByMedia((current) => ({
          ...current,
          [targetMediaType]: nextModels,
        }));
        setDrafts((current) => {
          const currentDraft = current[targetMediaType] ?? defaultSubmitValues[targetMediaType];
          const currentModelId =
            typeof currentDraft.modelId === "string" ? currentDraft.modelId : "";
          if (currentModelId && nextModels.some((option) => option.id === currentModelId)) {
            return current;
          }

          const preferredModel =
            nextModels.find((option) => option.isAvailable && option.isDefault) ??
            nextModels.find((option) => option.isAvailable) ??
            nextModels[0];
          if (!preferredModel) {
            return current;
          }

          return {
            ...current,
            [targetMediaType]: {
              ...currentDraft,
              mediaType: targetMediaType,
              modelId: preferredModel.id,
            },
          };
        });
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to load models.");
    }
  }

  async function loadAssetDetail(assetId: string): Promise<void> {
    try {
      const payload = await requestJson<GalleryAssetDetailResponse>(`/gallery/${assetId}`);
      startTransition(() => {
        setSelectedAssetId(payload.asset_id);
        setSelectedAssetDetail(payload);
        setSelectedAssetProjectId(payload.project_id ?? "");
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to load asset detail.");
    }
  }

  async function refreshStudio(
    targetMediaType: MediaType,
    options: RefreshStudioOptions = {},
  ): Promise<void> {
    try {
      const [galleryPayload, metricsPayload, galleryStatsPayload] = await Promise.all([
        requestJson<GalleryItemResponse[]>(
          `/gallery?media_type=${encodeURIComponent(targetMediaType)}&limit=8`,
        ),
        requestJson<MetricsSummaryResponse>("/metrics/summary"),
        requestJson<GalleryStatsResponse>("/gallery/stats"),
      ]);

      let detailPayload: GalleryAssetDetailResponse | null = null;
      let nextAssetId: string | null = null;

      if (options.preferredJobId) {
        try {
          detailPayload = await requestJson<GalleryAssetDetailResponse>(
            `/gallery/job/${options.preferredJobId}`,
          );
          nextAssetId = detailPayload.asset_id;
        } catch (error) {
          console.error(error);
        }
      }

      if (detailPayload === null) {
        const candidateAssetId =
          options.preferredAssetId && galleryPayload.some((item) => item.asset_id === options.preferredAssetId)
            ? options.preferredAssetId
            : galleryPayload.some((item) => item.asset_id === selectedAssetId)
              ? selectedAssetId
              : galleryPayload[0]?.asset_id ?? null;

        if (candidateAssetId) {
          detailPayload = await requestJson<GalleryAssetDetailResponse>(
            `/gallery/${candidateAssetId}`,
          );
          nextAssetId = candidateAssetId;
        }
      }

      startTransition(() => {
        setGalleryItems(galleryPayload);
        setMetrics(metricsPayload);
        setGalleryStats(galleryStatsPayload);
        setSelectedAssetId(nextAssetId);
        setSelectedAssetDetail(detailPayload);
        setSelectedAssetProjectId(detailPayload?.project_id ?? "");
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to refresh studio data.");
    }
  }

  async function loadJob(jobId: string, refreshAfterFinish = false): Promise<void> {
    try {
      const payload = await requestJson<JobResponse>(`/jobs/${jobId}`);
      startTransition(() => {
        setLatestJob(payload);
      });

      if (terminalStatuses.has(payload.status)) {
        setActiveJobId(null);
        setIsSubmitting(false);
        setStatusMessage(
          payload.status === "succeeded"
            ? "Generation finished."
            : payload.error_message || `Job ${payload.status}.`,
        );
        if (refreshAfterFinish) {
          await refreshStudio(payload.media_type, { preferredJobId: payload.id });
          await loadProjects();
        }
      } else {
        setStatusMessage(`Job ${payload.status}...`);
      }
    } catch (error) {
      setActiveJobId(null);
      setIsSubmitting(false);
      setErrorMessage(error instanceof Error ? error.message : "Failed to load job state.");
    }
  }

  async function handleSubmit(values: PromptFormSubmitValues): Promise<void> {
    setIsSubmitting(true);
    setErrorMessage(null);
    setStatusMessage("Queueing generation...");
    setAssetMessage(null);

    try {
      const created = await requestJson<CreateJobResponse>(`/generate/${values.mediaType}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(buildGeneratePayload(values, selectedProjectId || null)),
      });

      setActiveJobId(created.job_id);
      setStatusMessage(`Job ${created.job_id} queued.`);
      await loadJob(created.job_id);
    } catch (error) {
      setIsSubmitting(false);
      setErrorMessage(error instanceof Error ? error.message : "Failed to submit generation.");
      setStatusMessage(null);
    }
  }

  function loadSelectedAssetIntoComposer(): void {
    if (!selectedAssetDetail) {
      return;
    }

    const nextDraft = createDraftFromRequestSnapshot(selectedAssetDetail.request_snapshot);
    startTransition(() => {
      setDrafts((current) => ({
        ...current,
        [selectedAssetDetail.media_type]: {
          ...defaultSubmitValues[selectedAssetDetail.media_type],
          ...nextDraft,
        },
      }));
      setSelectedProjectId(selectedAssetDetail.project_id ?? "");
      setMediaType(selectedAssetDetail.media_type);
      setComposerRevision((current) => current + 1);
      setAssetMessage(`Loaded ${selectedAssetDetail.asset_id} into the composer.`);
    });
  }

  async function handleAssetReuse(): Promise<void> {
    if (!selectedAssetDetail) {
      return;
    }

    setIsAssetBusy(true);
    setErrorMessage(null);
    setAssetMessage(null);

    const sourceValues =
      mediaType === selectedAssetDetail.media_type
        ? mergeDraftWithDefaults(selectedAssetDetail.media_type, drafts[selectedAssetDetail.media_type])
        : mergeDraftWithDefaults(
            selectedAssetDetail.media_type,
            createDraftFromRequestSnapshot(selectedAssetDetail.request_snapshot),
          );

    try {
      const payload = await requestJson<ReuseAssetResponse>(
        `/gallery/${selectedAssetDetail.asset_id}/reuse`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(
            buildReusePayload(
              sourceValues,
              selectedProjectId || selectedAssetDetail.project_id || null,
            ),
          ),
        },
      );

      setMediaType(selectedAssetDetail.media_type);
      setSelectedProjectId(payload.project_id ?? "");
      setIsSubmitting(true);
      setActiveJobId(payload.job_id);
      setStatusMessage(`Queued variation job ${payload.job_id}.`);
      setAssetMessage(`Created a variation job from ${selectedAssetDetail.asset_id}.`);
      await loadJob(payload.job_id);
      await loadProjects();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to reuse the selected asset.");
    } finally {
      setIsAssetBusy(false);
    }
  }

  async function handleAssetExport(): Promise<void> {
    if (!selectedAssetDetail) {
      return;
    }

    setIsAssetBusy(true);
    setErrorMessage(null);

    try {
      const payload = await requestJson<ExportAssetResponse>(
        `/gallery/${selectedAssetDetail.asset_id}/export`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ include_metadata: true }),
        },
      );
      setAssetMessage(`Exported asset to ${payload.export_path}.`);
      await refreshStudio(selectedAssetDetail.media_type, {
        preferredAssetId: selectedAssetDetail.asset_id,
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to export the selected asset.");
    } finally {
      setIsAssetBusy(false);
    }
  }

  async function handleAssetProjectBinding(): Promise<void> {
    if (!selectedAssetDetail) {
      return;
    }

    setIsAssetBusy(true);
    setErrorMessage(null);

    try {
      const payload = await requestJson<GalleryAssetDetailResponse>(
        `/gallery/${selectedAssetDetail.asset_id}/project`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            project_id: selectedAssetProjectId || null,
          }),
        },
      );

      startTransition(() => {
        setSelectedAssetDetail(payload);
        setSelectedAssetId(payload.asset_id);
        setSelectedAssetProjectId(payload.project_id ?? "");
      });
      setSelectedProjectId(payload.project_id ?? "");
      setAssetMessage(
        payload.project_id
          ? `Bound asset ${payload.asset_id} to the selected project.`
          : `Removed project binding from asset ${payload.asset_id}.`,
      );
      await refreshStudio(payload.media_type, { preferredAssetId: payload.asset_id });
      await loadProjects();
    } catch (error) {
      setErrorMessage(
        error instanceof Error ? error.message : "Failed to update the asset project binding.",
      );
    } finally {
      setIsAssetBusy(false);
    }
  }

  return (
    <div className="app-shell app-shell--studio">
      <aside className="studio-sidebar">
        <section className="section-card section-card--nav">
          <div className="sidebar-brand">
            <p className="eyebrow">Creative AI Studio</p>
            <h1>Asset-aware local studio</h1>
            <p className="sidebar-copy">
              Compose prompts, inspect generated assets, and move from output to reuse
              without leaving the studio.
            </p>
          </div>
          <div className="theme-switch">
            <span className="theme-switch__label">Theme</span>
            <div className="theme-switch__group">
              <button
                type="button"
                className={`theme-switch__button ${themeMode === "light" ? "is-active" : ""}`}
                onClick={() => setThemeMode("light")}
              >
                Light
              </button>
              <button
                type="button"
                className={`theme-switch__button ${themeMode === "dark" ? "is-active" : ""}`}
                onClick={() => setThemeMode("dark")}
              >
                Dark
              </button>
            </div>
          </div>
        </section>

        <section className="section-card">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Surface</p>
              <h2>Choose a media lane</h2>
            </div>
          </div>
          <div className="surface-nav">
            {(["image", "audio", "video"] as MediaType[]).map((option) => (
              <button
                key={option}
                type="button"
                className={`surface-nav__item ${mediaType === option ? "is-active" : ""}`}
                onClick={() => setMediaType(option)}
              >
                <div className="surface-nav__topline">
                  <span className="surface-pill">{mediaTypeLabels[option]}</span>
                  <span className="surface-state surface-state--live">Live</span>
                </div>
                <strong>{mediaTypeLabels[option]} workflows</strong>
                <p className="sidebar-meta">
                  {option === "image"
                    ? "Render stills with SDXL and optional LoRA."
                    : option === "audio"
                      ? "Sketch loop ideas and audition playback."
                      : "Generate boards and learned-runtime variations."}
                </p>
              </button>
            ))}
          </div>
        </section>

        <section className="section-card section-card--snapshot">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Project Binding</p>
              <h2>Route the next job</h2>
            </div>
          </div>
          <label className="field-group field-group--full">
            <span>Composer project</span>
            <select
              value={selectedProjectId}
              onChange={(event) => setSelectedProjectId(event.target.value)}
            >
              <option value="">No project</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name} ({project.asset_count} assets)
                </option>
              ))}
            </select>
          </label>
          <p className="section-footnote">
            {projects.length > 0
              ? `${projects.length} projects can receive new jobs or reused assets.`
              : "Create a project through the API to start grouping jobs and assets."}
          </p>
        </section>

        <section className="section-card">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Asset Workflow</p>
              <h2>Library activity</h2>
            </div>
          </div>
          <div className="monitor-stack">
            <div className="metric-pill">
              <strong>{galleryStats?.total_items ?? 0}</strong>
              <p>Total tracked assets</p>
            </div>
            <div className="metric-pill">
              <strong>{galleryStats?.total_reuse_count ?? 0}</strong>
              <p>Reuse actions recorded</p>
            </div>
            <div className="metric-pill">
              <strong>{galleryStats?.total_export_count ?? 0}</strong>
              <p>Exports written out</p>
            </div>
          </div>
        </section>
      </aside>

      <main className="studio-main">
        {errorMessage ? <p className="error-banner">{errorMessage}</p> : null}

        <section className="section-card section-card--stage">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Composer</p>
              <h2>{mediaTypeLabels[mediaType]} generation</h2>
            </div>
            <p className="section-footnote">
              Model manifests and local availability are loaded live from the registry.
            </p>
          </div>
          <PromptForm
            key={`${mediaType}:${composerRevision}`}
            formId="studio-prompt-form"
            mediaType={mediaType}
            modelOptions={activeModels}
            loraOptions={mediaType === "image" ? loraOptions : []}
            initialValues={drafts[mediaType]}
            submitLabel={isSubmitting ? "Generating..." : "Queue generation"}
            disabled={isSubmitting}
            canSubmit={!isSubmitting}
            statusMessage={statusMessage}
            onDraftChange={(nextDraft) =>
              setDrafts((current) => ({
                ...current,
                [mediaType]: nextDraft,
              }))
            }
            onSubmit={(values) => {
              void handleSubmit(values);
            }}
          />
        </section>

        <div className="workspace-grid">
          <section className="section-card section-card--monitor">
            <div className="section-card__header">
              <div>
                <p className="eyebrow">Latest Job</p>
                <h2>Run state and output preview</h2>
              </div>
            </div>
            {latestJob ? (
              <div className="monitor-stack">
                <div className="gallery-item__topline">
                  <span className={`status-chip status-chip--${latestJob.status}`}>
                    {latestJob.status}
                  </span>
                  <span className="history-score">{formatPercent(latestJob.progress * 100)}</span>
                </div>
                <StagePreview
                  mediaType={latestJob.media_type}
                  outputPath={latestJob.result?.previews[0] ?? latestJob.result?.outputs[0] ?? null}
                  title={latestJob.request.prompt}
                  subtitle={latestJob.request.model_id || "default"}
                />
                <div className="metadata-grid">
                  <div className="metadata-item">
                    <span>Prompt</span>
                    <strong>{latestJob.request.prompt}</strong>
                  </div>
                  <div className="metadata-item">
                    <span>Project</span>
                    <strong>{latestJob.project_id ?? "Unassigned"}</strong>
                  </div>
                  <div className="metadata-item">
                    <span>Quality</span>
                    <strong>{formatScore(extractJobQualityScore(latestJob))}</strong>
                  </div>
                  <div className="metadata-item">
                    <span>Updated</span>
                    <strong>{formatDate(latestJob.updated_at)}</strong>
                  </div>
                </div>
              </div>
            ) : (
              <div className="empty-stage">
                <div>
                  <h3>No job selected</h3>
                  <p>Queue a generation or reuse an existing asset to populate the stage.</p>
                </div>
              </div>
            )}
          </section>

          <section className="section-card section-card--gallery">
            <div className="section-card__header">
              <div>
                <p className="eyebrow">Gallery</p>
                <h2>Recent {mediaTypeLabels[mediaType].toLowerCase()} assets</h2>
              </div>
              <p className="section-footnote">{galleryItems.length} items loaded</p>
            </div>
            <div className="gallery-list">
              {galleryItems.length > 0 ? (
                galleryItems.map((item) => (
                  <button
                    key={item.asset_id}
                    type="button"
                    className={`gallery-item ${item.asset_id === selectedAssetId ? "is-active" : ""}`}
                    onClick={() => {
                      void loadAssetDetail(item.asset_id);
                    }}
                  >
                    <OutputThumbnail mediaType={item.media_type} outputPath={item.preview_path} />
                    <div className="gallery-item__body">
                      <div className="gallery-item__topline">
                        <span className="history-item__media">{item.project_name || "Unassigned"}</span>
                        <span className="history-score">
                          {formatScore(item.quality_score_calibrated ?? item.quality_score)}
                        </span>
                      </div>
                      <strong>{item.prompt}</strong>
                      <p>
                        {item.model_id} | feedback {item.feedback_count} | reuse {item.reuse_count} |
                        export {item.export_count}
                      </p>
                    </div>
                  </button>
                ))
              ) : (
                <div className="history-empty">
                  Successful jobs will appear here after the runner finishes them.
                </div>
              )}
            </div>
          </section>
        </div>

        <section className="section-card">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Selected Asset</p>
              <h2>Inspect, bind, export, and reuse</h2>
            </div>
            {assetMessage ? <p className="section-footnote">{assetMessage}</p> : null}
          </div>
          {selectedAssetDetail ? (
            <div className="detail-grid">
              <div className="stage-stack">
                <StagePreview
                  mediaType={selectedAssetDetail.media_type}
                  outputPath={selectedAssetDetail.preview_path}
                  title={selectedAssetDetail.prompt}
                  subtitle={selectedAssetDetail.project_name || "Unassigned"}
                />
                <div className="asset-actions">
                  <button
                    type="button"
                    className="dock-submit"
                    onClick={() => {
                      void handleAssetReuse();
                    }}
                    disabled={isAssetBusy}
                  >
                    Reuse and rerun
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={loadSelectedAssetIntoComposer}
                    disabled={isAssetBusy}
                  >
                    Load into composer
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => {
                      void handleAssetExport();
                    }}
                    disabled={isAssetBusy}
                  >
                    Export asset
                  </button>
                </div>
                <div className="form-section">
                  <div className="form-section__header">
                    <div>
                      <p className="eyebrow">Project Binding</p>
                      <h3>Move the source job and asset between projects</h3>
                    </div>
                  </div>
                  <label className="field-group field-group--full">
                    <span>Asset project</span>
                    <select
                      value={selectedAssetProjectId}
                      onChange={(event) => setSelectedAssetProjectId(event.target.value)}
                      disabled={isAssetBusy}
                    >
                      <option value="">Unassigned</option>
                      {projects.map((project) => (
                        <option key={project.id} value={project.id}>
                          {project.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => {
                      void handleAssetProjectBinding();
                    }}
                    disabled={isAssetBusy}
                  >
                    Update asset project
                  </button>
                </div>
              </div>

              <div className="monitor-stack">
                <div className="metadata-grid">
                  <div className="metadata-item">
                    <span>Asset</span>
                    <strong>{selectedAssetDetail.asset_id}</strong>
                  </div>
                  <div className="metadata-item">
                    <span>Model</span>
                    <strong>{selectedAssetDetail.model_id}</strong>
                  </div>
                  <div className="metadata-item">
                    <span>Created</span>
                    <strong>{formatDate(selectedAssetDetail.created_at)}</strong>
                  </div>
                  <div className="metadata-item">
                    <span>Updated</span>
                    <strong>{formatDate(selectedAssetDetail.updated_at)}</strong>
                  </div>
                  <div className="metadata-item">
                    <span>Quality</span>
                    <strong>
                      {formatScore(
                        selectedAssetDetail.quality_score_calibrated ??
                          selectedAssetDetail.quality_score,
                      )}
                    </strong>
                  </div>
                  <div className="metadata-item">
                    <span>Semantic</span>
                    <strong>
                      {formatScore(
                        selectedAssetDetail.semantic_alignment_score_calibrated ??
                          selectedAssetDetail.semantic_alignment_score,
                      )}
                    </strong>
                  </div>
                  <div className="metadata-item">
                    <span>Creative</span>
                    <strong>
                      {formatScore(
                        selectedAssetDetail.creative_alignment_score_calibrated ??
                          selectedAssetDetail.creative_alignment_score,
                      )}
                    </strong>
                  </div>
                  <div className="metadata-item">
                    <span>Feedback</span>
                    <strong>{selectedAssetDetail.feedback_count}</strong>
                  </div>
                </div>

                <div className="form-section">
                  <div className="form-section__header">
                    <div>
                      <p className="eyebrow">Lineage</p>
                      <h3>Trace reuse and export state</h3>
                    </div>
                  </div>
                  <div className="asset-chip-row">
                    <span className="form-section__mode">
                      reuse {selectedAssetDetail.reuse_count}
                    </span>
                    <span className="form-section__mode">
                      export {selectedAssetDetail.export_count}
                    </span>
                    <span className="form-section__mode">
                      feedback {selectedAssetDetail.feedback_count}
                    </span>
                  </div>
                  <div className="asset-list">
                    <div className="asset-path">
                      <span>Parent Asset</span>
                      <code>{selectedAssetDetail.parent_asset_id ?? "none"}</code>
                    </div>
                    <div className="asset-path">
                      <span>Lineage</span>
                      <code>
                        {selectedAssetDetail.lineage.length > 0
                          ? selectedAssetDetail.lineage.join(", ")
                          : "none"}
                      </code>
                    </div>
                    <div className="asset-path">
                      <span>Exports</span>
                      <code>
                        {selectedAssetDetail.export_paths.length > 0
                          ? selectedAssetDetail.export_paths.join(", ")
                          : "none"}
                      </code>
                    </div>
                  </div>
                </div>

                <pre>{JSON.stringify(selectedAssetDetail.request_snapshot, null, 2)}</pre>
              </div>
            </div>
          ) : (
            <div className="empty-stage">
              <div>
                <h3>No asset selected</h3>
                <p>Select an item from the gallery to inspect its detail and actions.</p>
              </div>
            </div>
          )}
        </section>

        <section className="section-card section-card--models">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Coverage</p>
              <h2>Models and operational summary</h2>
            </div>
          </div>
          <div className="workspace-grid">
            <div className="model-shelf">
              {activeModels.map((model) => (
                <div key={model.id} className="metric-pill">
                  <strong>{model.displayName}</strong>
                  <p>
                    {model.isAvailable ? "Installed" : "Manifest only"}
                    {model.tags.length > 0 ? ` | ${model.tags.join(", ")}` : ""}
                  </p>
                </div>
              ))}
            </div>
            <div className="monitor-stack">
              <div className="metric-pill">
                <strong>{metrics?.total_jobs ?? 0}</strong>
                <p>Total jobs</p>
              </div>
              <div className="metric-pill">
                <strong>{formatPercent(metrics?.success_rate)}</strong>
                <p>Studio success rate</p>
              </div>
              <div className="metric-pill">
                <strong>{formatScore(activeMetrics?.average_quality_score)}</strong>
                <p>{mediaTypeLabels[mediaType]} average quality</p>
              </div>
              <div className="metric-pill">
                <strong>{formatPercent(activeMetrics?.feedback_coverage_rate)}</strong>
                <p>{mediaTypeLabels[mediaType]} feedback coverage</p>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

type StagePreviewProps = {
  mediaType: MediaType;
  outputPath: string | null;
  title: string;
  subtitle: string;
};

function StagePreview({ mediaType, outputPath, title, subtitle }: StagePreviewProps) {
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="stage-surface">
        <div className="empty-stage">
          <div>
            <h3>Preview unavailable</h3>
            <p>{outputPath ?? "No output path was returned by the API."}</p>
          </div>
        </div>
      </div>
    );
  }

  if (mediaType === "audio" || isAudioAsset(outputPath)) {
    return (
      <div className="stage-surface stage-surface--audio">
        <div className="audio-preview">
          <div className="audio-preview__header">
            <p className="eyebrow">Audio Preview</p>
            <strong>{title}</strong>
            <p className="sidebar-copy">{subtitle}</p>
          </div>
          <audio controls preload="metadata" src={src} />
        </div>
      </div>
    );
  }

  if (isVideoAsset(outputPath)) {
    return (
      <div className="stage-surface stage-surface--hero">
        <video controls muted playsInline preload="metadata" src={src} />
      </div>
    );
  }

  return (
    <div className="stage-surface stage-surface--hero">
      <img src={src} alt={title} loading="lazy" />
    </div>
  );
}

type OutputThumbnailProps = {
  mediaType: MediaType;
  outputPath: string | null;
};

function OutputThumbnail({ mediaType, outputPath }: OutputThumbnailProps) {
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">None</span>
      </div>
    );
  }

  if (mediaType === "audio" || isAudioAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">Audio</span>
      </div>
    );
  }

  if (isVideoAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb">
        <video muted playsInline preload="metadata" src={src} />
      </div>
    );
  }

  return (
    <div className="gallery-item__thumb">
      <img src={src} alt="" loading="lazy" />
    </div>
  );
}

export default App;
