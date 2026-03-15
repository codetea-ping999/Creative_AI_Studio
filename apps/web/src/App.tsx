import { useEffect, useRef, useState } from "react";
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

type QualityReport = {
  method: string;
  quality_score: number;
  quality_level: string;
  business_readiness_score?: number;
  business_readiness_level?: string;
  semantic_alignment_score?: number;
  semantic_alignment_level?: string;
  creative_alignment_score?: number;
  creative_alignment_level?: string;
  checks?: string[];
  metrics?: Record<string, unknown>;
  notes?: string[];
  semantic_report?: Record<string, unknown>;
};

type GenerationResult = {
  outputs: string[];
  previews: string[];
  metadata: Record<string, unknown>;
};

type JobResponse = {
  id: string;
  media_type: MediaType;
  status: JobStatus;
  progress: number;
  error_message: string | null;
  request: {
    prompt: string;
    negative_prompt: string | null;
    model_id: string;
    seed: number | null;
    params: Record<string, unknown>;
  };
  result: GenerationResult | null;
  created_at: string;
  updated_at: string;
};

type ModelsResponse = {
  models: Array<{
    id: string;
    display_name: string;
    default_params: Record<string, unknown>;
    tags: string[];
    is_default: boolean;
    is_available: boolean;
  }>;
};

type LoraCatalogResponse = {
  root: string;
  items: LoraOption[];
};

type MediaMetrics = {
  total_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  running_jobs: number;
  success_rate: number;
  save_success_rate: number;
  average_quality_score: number | null;
  average_business_readiness_score: number | null;
  average_semantic_alignment_score: number | null;
  latest_quality_level: string | null;
};

type MetricsSummaryResponse = {
  total_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  running_jobs: number;
  success_rate: number;
  save_success_rate: number;
  average_quality_score: number | null;
  average_business_readiness_score: number | null;
  average_semantic_alignment_score: number | null;
  latest_quality_level: string | null;
  recent_window_size: number;
  recent_success_rate: number;
  recent_average_quality_score: number | null;
  by_media: Record<string, MediaMetrics>;
};

type GalleryItemResponse = {
  job_id: string;
  project_id: string | null;
  media_type: MediaType;
  prompt: string;
  model_id: string;
  output_path: string;
  preview_path: string | null;
  created_at: string;
  quality_score: number | null;
  quality_level: string | null;
  success: boolean;
};

type ThemeMode = "light" | "dark";

const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";

const terminalStatuses = new Set<JobStatus>(["succeeded", "failed", "cancelled"]);

const draftDefaults: Record<MediaType, Partial<PromptFormSubmitValues>> = {
  image: {
    mediaType: "image",
    prompt: "",
    negativePrompt: "",
    width: 1024,
    height: 1024,
    steps: 30,
    guidanceScale: 7.5,
    loraPath: "",
    loraScale: 0.8,
    seed: null,
  },
  audio: {
    mediaType: "audio",
    modelId: "musicgen-small",
    prompt: "",
    durationSeconds: 8,
    guidanceScale: 3,
    bpm: 96,
    mood: "dreamy",
    seed: null,
  },
  video: {
    mediaType: "video",
    modelId: "storyboard-video",
    prompt: "",
    negativePrompt: "",
    width: 576,
    height: 320,
    durationSeconds: 4,
    cameraMotion: "push-in",
    visualStyle: "storyboard",
    seed: null,
  },
};

const creativeSurfaces: Array<{
  id: string;
  label: string;
  description: string;
  status: "live" | "soon";
  mediaType?: MediaType;
}> = [
  {
    id: "image",
    label: "Image",
    description: "text-to-image, LoRA styling, art direction",
    status: "live",
    mediaType: "image",
  },
  {
    id: "video",
    label: "Video",
    description: "motion pipeline and storyboard workflows",
    status: "live",
    mediaType: "video",
  },
  {
    id: "ai-app",
    label: "AI App",
    description: "guided utilities and one-click creation flows",
    status: "soon",
  },
  {
    id: "agent",
    label: "Agent",
    description: "assistive prompting and workflow copilots",
    status: "soon",
  },
  {
    id: "character",
    label: "Character",
    description: "persona memory, identity and pose continuity",
    status: "soon",
  },
  {
    id: "song",
    label: "Song",
    description: "loop sketch, mood generation, playback review",
    status: "live",
    mediaType: "audio",
  },
  {
    id: "comic",
    label: "Comic",
    description: "panel layout, page sequencing, speech bubble pass",
    status: "soon",
  },
];

function resolveInitialTheme(): ThemeMode {
  if (typeof window === "undefined") {
    return "dark";
  }

  const storedTheme = window.localStorage.getItem("creative-ai-studio-theme");
  if (storedTheme === "light" || storedTheme === "dark") {
    return storedTheme;
  }

  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`${path} request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

function toAssetUrl(path: string): string {
  if (/^https?:\/\//.test(path)) {
    return path;
  }

  const normalizedPath = path.replace(/\\/g, "/");
  const outputsIndex = normalizedPath.lastIndexOf("/outputs/");
  if (outputsIndex >= 0) {
    return `${API_BASE_URL}${normalizedPath.slice(outputsIndex)}`;
  }
  if (normalizedPath.startsWith("outputs/")) {
    return `${API_BASE_URL}/${normalizedPath}`;
  }
  if (normalizedPath.startsWith("/outputs/")) {
    return `${API_BASE_URL}${normalizedPath}`;
  }
  return `${API_BASE_URL}${normalizedPath.startsWith("/") ? normalizedPath : `/${normalizedPath}`}`;
}

function sortJobsByUpdatedAt(jobs: JobResponse[]): JobResponse[] {
  return [...jobs].sort(
    (left, right) =>
      new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime(),
  );
}

function mergeRecentJobs(currentJobs: JobResponse[], nextJob: JobResponse): JobResponse[] {
  const remainingJobs = currentJobs.filter((job) => job.id !== nextJob.id);
  return sortJobsByUpdatedAt([nextJob, ...remainingJobs]).slice(0, 8);
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("ja-JP", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function summarizePrompt(prompt: string, fallback: string): string {
  const compact = prompt.trim().replace(/\s+/g, " ");
  if (compact.length === 0) {
    return fallback;
  }
  if (compact.length <= 88) {
    return compact;
  }
  return `${compact.slice(0, 85)}...`;
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "-";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function formatPercent(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "-";
  }
  return `${value.toFixed(1)}%`;
}

function formatScore(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "-";
  }
  return value.toFixed(1);
}

function createRequestSnapshot(
  mediaType: MediaType,
  drafts: Record<MediaType, Partial<PromptFormSubmitValues>>,
  defaultImageModelId: string,
  defaultAudioModelId: string,
  defaultVideoModelId: string,
): PromptFormSubmitValues {
  const imageDraft = drafts.image;
  const audioDraft = drafts.audio;
  const videoDraft = drafts.video;
  const activeDraft = drafts[mediaType];

  return {
    mediaType,
    modelId:
      mediaType === "image"
        ? typeof imageDraft.modelId === "string" && imageDraft.modelId.length > 0
          ? imageDraft.modelId
          : defaultImageModelId
        : mediaType === "audio"
          ? typeof audioDraft.modelId === "string" && audioDraft.modelId.length > 0
            ? audioDraft.modelId
            : defaultAudioModelId
          : typeof videoDraft.modelId === "string" && videoDraft.modelId.length > 0
            ? videoDraft.modelId
            : defaultVideoModelId,
    prompt: typeof activeDraft.prompt === "string" ? activeDraft.prompt : "",
    negativePrompt:
      mediaType === "image"
        ? typeof imageDraft.negativePrompt === "string"
          ? imageDraft.negativePrompt
          : ""
        : typeof videoDraft.negativePrompt === "string"
          ? videoDraft.negativePrompt
          : "",
    width:
      mediaType === "video"
        ? typeof videoDraft.width === "number"
          ? videoDraft.width
          : 576
        : typeof imageDraft.width === "number"
          ? imageDraft.width
          : 1024,
    height:
      mediaType === "video"
        ? typeof videoDraft.height === "number"
          ? videoDraft.height
          : 320
        : typeof imageDraft.height === "number"
          ? imageDraft.height
          : 1024,
    steps: typeof imageDraft.steps === "number" ? imageDraft.steps : 30,
    guidanceScale:
      typeof imageDraft.guidanceScale === "number" ? imageDraft.guidanceScale : 7.5,
    loraPath: typeof imageDraft.loraPath === "string" ? imageDraft.loraPath : "",
    loraScale: typeof imageDraft.loraScale === "number" ? imageDraft.loraScale : 0.8,
    seed: typeof activeDraft.seed === "number" ? activeDraft.seed : null,
    durationSeconds:
      mediaType === "audio"
        ? typeof audioDraft.durationSeconds === "number"
          ? audioDraft.durationSeconds
          : 8
        : typeof videoDraft.durationSeconds === "number"
          ? videoDraft.durationSeconds
          : 4,
    bpm: typeof audioDraft.bpm === "number" ? audioDraft.bpm : 96,
    mood: typeof audioDraft.mood === "string" ? audioDraft.mood : "dreamy",
    cameraMotion:
      typeof videoDraft.cameraMotion === "string" ? videoDraft.cameraMotion : "push-in",
    visualStyle:
      typeof videoDraft.visualStyle === "string" ? videoDraft.visualStyle : "storyboard",
  };
}

function extractQualityReport(job: JobResponse | null): QualityReport | null {
  const candidate = job?.result?.metadata?.quality_report;
  if (!candidate || typeof candidate !== "object") {
    return null;
  }

  const quality = candidate as Record<string, unknown>;
  if (typeof quality.quality_score !== "number") {
    return null;
  }

  return {
    method: typeof quality.method === "string" ? quality.method : "heuristic_local_v1",
    quality_score: quality.quality_score,
    quality_level:
      typeof quality.quality_level === "string" ? quality.quality_level : "unknown",
    business_readiness_score:
      typeof quality.business_readiness_score === "number"
        ? quality.business_readiness_score
        : undefined,
    business_readiness_level:
      typeof quality.business_readiness_level === "string"
        ? quality.business_readiness_level
        : undefined,
    semantic_alignment_score:
      typeof quality.semantic_alignment_score === "number"
        ? quality.semantic_alignment_score
        : undefined,
    semantic_alignment_level:
      typeof quality.semantic_alignment_level === "string"
        ? quality.semantic_alignment_level
        : undefined,
    creative_alignment_score:
      typeof quality.creative_alignment_score === "number"
        ? quality.creative_alignment_score
        : undefined,
    creative_alignment_level:
      typeof quality.creative_alignment_level === "string"
        ? quality.creative_alignment_level
        : undefined,
    checks: Array.isArray(quality.checks)
      ? quality.checks.filter((item): item is string => typeof item === "string")
      : [],
    metrics:
      quality.metrics && typeof quality.metrics === "object"
        ? (quality.metrics as Record<string, unknown>)
        : {},
    notes: Array.isArray(quality.notes)
      ? quality.notes.filter((item): item is string => typeof item === "string")
      : [],
    semantic_report:
      quality.semantic_report && typeof quality.semantic_report === "object"
        ? (quality.semantic_report as Record<string, unknown>)
        : undefined,
  };
}

export default function App() {
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => resolveInitialTheme());
  const [mediaType, setMediaType] = useState<MediaType>("image");
  const [isComposerCollapsed, setIsComposerCollapsed] = useState(false);
  const [drafts, setDrafts] =
    useState<Record<MediaType, Partial<PromptFormSubmitValues>>>(draftDefaults);
  const [lastSubmission, setLastSubmission] =
    useState<PromptFormSubmitValues | null>(null);
  const [job, setJob] = useState<JobResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [imageModels, setImageModels] = useState<ModelOption[]>([]);
  const [audioModels, setAudioModels] = useState<ModelOption[]>([]);
  const [videoModels, setVideoModels] = useState<ModelOption[]>([]);
  const [loraOptions, setLoraOptions] = useState<LoraOption[]>([]);
  const [recentJobs, setRecentJobs] = useState<JobResponse[]>([]);
  const [galleryItems, setGalleryItems] = useState<GalleryItemResponse[]>([]);
  const [metricsSummary, setMetricsSummary] = useState<MetricsSummaryResponse | null>(null);
  const pollTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearTimeout(pollTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = themeMode;
    document.documentElement.style.colorScheme = themeMode;
    window.localStorage.setItem("creative-ai-studio-theme", themeMode);
  }, [themeMode]);

  useEffect(() => {
    const bootstrapStudio = async () => {
      const results = await Promise.allSettled([
        fetchJson<ModelsResponse>("/models?media_type=image"),
        fetchJson<ModelsResponse>("/models?media_type=audio"),
        fetchJson<ModelsResponse>("/models?media_type=video"),
        fetchJson<LoraCatalogResponse>("/catalog/loras"),
        fetchJson<MetricsSummaryResponse>("/metrics/summary"),
        fetchJson<JobResponse[]>("/jobs"),
        fetchJson<GalleryItemResponse[]>("/gallery?limit=24"),
      ]);

      const errors = results
        .filter((result): result is PromiseRejectedResult => result.status === "rejected")
        .map((result) => (result.reason instanceof Error ? result.reason.message : String(result.reason)));

      const imageModelsResult = results[0];
      const audioModelsResult = results[1];
      const videoModelsResult = results[2];
      const loraCatalogResult = results[3];
      const metricsResult = results[4];
      const jobsResult = results[5];
      const galleryResult = results[6];

      if (
        imageModelsResult.status === "fulfilled" &&
        audioModelsResult.status === "fulfilled" &&
        videoModelsResult.status === "fulfilled"
      ) {
        const serializeModels = (payload: ModelsResponse) =>
          payload.models.map((model) => ({
            id: model.id,
            displayName: model.display_name,
            defaultParams: model.default_params,
            tags: model.tags,
            isAvailable: model.is_available,
            isDefault: model.is_default,
          }));

        const nextImageModels = serializeModels(imageModelsResult.value);
        const nextAudioModels = serializeModels(audioModelsResult.value);
        const nextVideoModels = serializeModels(videoModelsResult.value);

        setImageModels(nextImageModels);
        setAudioModels(nextAudioModels);
        setVideoModels(nextVideoModels);

        const defaultImageModelId =
          nextImageModels.find((model) => model.isDefault && model.isAvailable)?.id ??
          nextImageModels.find((model) => model.isAvailable)?.id ??
          "sdxl";
        const defaultAudioModelId =
          nextAudioModels.find((model) => model.isDefault && model.isAvailable)?.id ??
          nextAudioModels.find((model) => model.isAvailable)?.id ??
          "musicgen-small";
        const defaultVideoModelId =
          nextVideoModels.find((model) => model.isDefault && model.isAvailable)?.id ??
          nextVideoModels.find((model) => model.isAvailable)?.id ??
          "storyboard-video";

        setDrafts((current) => ({
          ...current,
          image: {
            ...current.image,
            modelId:
              typeof current.image.modelId === "string" && current.image.modelId.length > 0
                ? current.image.modelId
                : defaultImageModelId,
          },
          audio: {
            ...current.audio,
            modelId:
              typeof current.audio.modelId === "string" && current.audio.modelId.length > 0
                ? current.audio.modelId
                : defaultAudioModelId,
          },
          video: {
            ...current.video,
            modelId:
              typeof current.video.modelId === "string" && current.video.modelId.length > 0
                ? current.video.modelId
                : defaultVideoModelId,
          },
        }));
      }

      if (loraCatalogResult.status === "fulfilled") {
        setLoraOptions(loraCatalogResult.value.items);
      }

      if (metricsResult.status === "fulfilled") {
        setMetricsSummary(metricsResult.value);
      }

      if (jobsResult.status === "fulfilled") {
        setRecentJobs(sortJobsByUpdatedAt(jobsResult.value).slice(0, 8));
      }
      if (galleryResult.status === "fulfilled") {
        setGalleryItems(galleryResult.value);
      }

      if (errors.length > 0) {
        setRequestError(errors.join(" / "));
      }
    };

    void bootstrapStudio();
  }, []);

  const refreshOperationalState = async () => {
    const results = await Promise.allSettled([
      fetchJson<MetricsSummaryResponse>("/metrics/summary"),
      fetchJson<JobResponse[]>("/jobs"),
      fetchJson<GalleryItemResponse[]>("/gallery?limit=24"),
    ]);

    const metricsResult = results[0];
    const jobsResult = results[1];
    const galleryResult = results[2];

    if (metricsResult.status === "fulfilled") {
      setMetricsSummary(metricsResult.value);
    }
    if (jobsResult.status === "fulfilled") {
      setRecentJobs(sortJobsByUpdatedAt(jobsResult.value).slice(0, 8));
    }
    if (galleryResult.status === "fulfilled") {
      setGalleryItems(galleryResult.value);
    }
    const refreshErrors = results
      .filter((result): result is PromiseRejectedResult => result.status === "rejected")
      .map((result) => (result.reason instanceof Error ? result.reason.message : String(result.reason)));
    if (refreshErrors.length > 0) {
      setRequestError((current) => current ?? refreshErrors.join(" / "));
    }
  };

  const defaultImageModelId =
    imageModels.find((model) => model.isDefault && model.isAvailable)?.id ??
    imageModels.find((model) => model.isAvailable)?.id ??
    "sdxl";
  const defaultAudioModelId =
    audioModels.find((model) => model.isDefault && model.isAvailable)?.id ??
    audioModels.find((model) => model.isAvailable)?.id ??
    "musicgen-small";
  const defaultVideoModelId =
    videoModels.find((model) => model.isDefault && model.isAvailable)?.id ??
    videoModels.find((model) => model.isAvailable)?.id ??
    "storyboard-video";

  const selectedImageModelId =
    typeof drafts.image.modelId === "string" && drafts.image.modelId.length > 0
      ? drafts.image.modelId
      : defaultImageModelId;
  const selectedAudioModelId =
    typeof drafts.audio.modelId === "string" && drafts.audio.modelId.length > 0
      ? drafts.audio.modelId
      : defaultAudioModelId;
  const selectedVideoModelId =
    typeof drafts.video.modelId === "string" && drafts.video.modelId.length > 0
      ? drafts.video.modelId
      : defaultVideoModelId;

  const selectedImageModel =
    imageModels.find((model) => model.id === selectedImageModelId) ?? null;
  const selectedAudioModel =
    audioModels.find((model) => model.id === selectedAudioModelId) ?? null;
  const selectedVideoModel =
    videoModels.find((model) => model.id === selectedVideoModelId) ?? null;

  const activeModelOptions =
    mediaType === "image"
      ? imageModels
      : mediaType === "audio"
        ? audioModels
        : videoModels;
  const activeAvailableModels = activeModelOptions.filter((model) => model.isAvailable);
  const canSubmit = activeAvailableModels.length > 0;
  const selectionStatusMessage =
    activeAvailableModels.length === 0
      ? mediaType === "image"
        ? "画像モデルが未配置です。`models/manifests` と runtime 配置を確認してください。"
        : mediaType === "audio"
          ? "音楽モデルが未配置です。MusicGen runtime の配置を確認してください。"
          : "動画 runtime が未配置です。`models/video/procedural` と manifest を確認してください。"
      : mediaType === "image" && loraOptions.length > 0
        ? `LoRA catalog ready: ${loraOptions.length} item(s)`
        : mediaType === "image"
          ? "LoRA は手入力または catalog 未配置です。"
          : mediaType === "audio"
            ? "品質評価は生成後に自動で集計されます。"
            : "storyboard gif をローカル生成して preview と quality を即確認できます。";

  const submitGeneration = async (values: PromptFormSubmitValues) => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }

    setDrafts((current) => ({
      ...current,
      [values.mediaType]: values,
    }));
    setLastSubmission(values);
    setJob(null);
    setRequestError(null);
    setIsSubmitting(true);

    try {
      const endpoint =
        values.mediaType === "audio"
          ? "/generate/audio"
          : values.mediaType === "video"
            ? "/generate/video"
            : "/generate/image";
      const payload =
        values.mediaType === "audio"
          ? {
              prompt: values.prompt,
              model_id: values.modelId,
              seed: values.seed,
              output_format: "wav",
              params: {
                duration_seconds: values.durationSeconds,
                guidance_scale: values.guidanceScale,
                bpm: values.bpm,
                mood: values.mood,
              },
            }
          : values.mediaType === "video"
            ? {
                prompt: values.prompt,
                negative_prompt: values.negativePrompt || null,
                model_id: values.modelId,
                seed: values.seed,
                output_format: "gif",
                params: {
                  width: values.width,
                  height: values.height,
                  duration_seconds: values.durationSeconds,
                  camera_motion: values.cameraMotion,
                  visual_style: values.visualStyle,
                },
              }
          : {
              prompt: values.prompt,
              negative_prompt: values.negativePrompt || null,
              model_id: values.modelId,
              seed: values.seed,
              output_format: "png",
              params: {
                width: values.width,
                height: values.height,
                steps: values.steps,
                guidance_scale: values.guidanceScale,
                lora_path: values.loraPath || undefined,
                lora_scale: values.loraScale,
              },
            };

      const createResponse = await fetch(`${API_BASE_URL}${endpoint}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });

      if (!createResponse.ok) {
        throw new Error(`generation request failed: ${createResponse.status}`);
      }

      const createdJob = (await createResponse.json()) as {
        job_id: string;
      };

      const pollJob = async () => {
        const nextJob = await fetchJson<JobResponse>(`/jobs/${createdJob.job_id}`);
        setJob(nextJob);
        setRecentJobs((current) => mergeRecentJobs(current, nextJob));

        if (!terminalStatuses.has(nextJob.status)) {
          pollTimerRef.current = window.setTimeout(() => {
            void pollJob();
          }, 500);
          return;
        }

        pollTimerRef.current = null;
        setIsSubmitting(false);
        await refreshOperationalState();
      };

      await pollJob();
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Unknown generation error";
      setRequestError(message);
      setIsSubmitting(false);
    }
  };

  const focusJob = async (jobId: string, nextMediaType: MediaType) => {
    try {
      const nextJob = await fetchJson<JobResponse>(`/jobs/${jobId}`);
      setJob(nextJob);
      setMediaType(nextMediaType);
      setRecentJobs((current) => mergeRecentJobs(current, nextJob));
      setRequestError(null);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Unknown job lookup error";
      setRequestError(message);
    }
  };

  const activeStageJob =
    job ??
    recentJobs.find((recentJob) => recentJob.media_type === mediaType) ??
    recentJobs[0] ??
    null;
  const outputPath = activeStageJob?.result?.outputs[0] ?? null;
  const outputUrl = outputPath ? toAssetUrl(outputPath) : null;
  const isAudioStage = activeStageJob?.media_type === "audio";
  const isVideoStage = activeStageJob?.media_type === "video";
  const qualityReport = extractQualityReport(activeStageJob);
  const qualityMetricsEntries = Object.entries(qualityReport?.metrics ?? {}).slice(0, 8);
  const metadataEntries = Object.entries(activeStageJob?.result?.metadata ?? {})
    .filter(([key]) => key !== "quality_report")
    .slice(0, 8);
  const activeStageParams =
    activeStageJob?.request.params && typeof activeStageJob.request.params === "object"
      ? activeStageJob.request.params
      : {};
  const stageProgressPercent = activeStageJob
    ? Math.max(0, Math.min(100, Math.round(activeStageJob.progress * 100)))
    : 0;
  const isStageJobInFlight =
    activeStageJob !== null && !terminalStatuses.has(activeStageJob.status);
  const audioStageDuration =
    typeof activeStageParams.duration_seconds === "number"
      ? `${activeStageParams.duration_seconds}s`
      : null;
  const audioStageBpm =
    typeof activeStageParams.bpm === "number" ? `${activeStageParams.bpm} BPM` : null;
  const audioStageMood =
    typeof activeStageParams.mood === "string" ? activeStageParams.mood : null;

  const requestSnapshot =
    lastSubmission ??
    createRequestSnapshot(
      mediaType,
      drafts,
      defaultImageModelId,
      defaultAudioModelId,
      defaultVideoModelId,
    );

  const runningJobs = metricsSummary?.running_jobs ?? recentJobs.filter(
    (recentJob) => !terminalStatuses.has(recentJob.status),
  ).length;
  const availableImageModels = imageModels.filter((model) => model.isAvailable).length;
  const availableAudioModels = audioModels.filter((model) => model.isAvailable).length;
  const availableVideoModels = videoModels.filter((model) => model.isAvailable).length;
  const selectedMediaMetrics = metricsSummary?.by_media[mediaType] ?? null;
  const semanticReportStatus =
    typeof qualityReport?.semantic_report?.status === "string"
      ? qualityReport.semantic_report.status
      : "unknown";
  const semanticReportReason =
    typeof qualityReport?.semantic_report?.reason === "string"
      ? qualityReport.semantic_report.reason
      : null;
  const liveSurfaceCount = creativeSurfaces.filter((surface) => surface.status === "live").length;
  const roadmapSurfaceCount = creativeSurfaces.length - liveSurfaceCount;
  const activeSurfaceLabel =
    mediaType === "image"
      ? "Image Studio"
      : mediaType === "audio"
        ? "Song Studio"
        : "Video Studio";
  const activeSurfaceSummary =
    mediaType === "image"
      ? "prompt, model, ratio, and LoRA direction"
      : mediaType === "audio"
        ? "prompt, mood, bpm, and playback direction"
        : "prompt, framing, duration, and camera direction";
  const activityWindowCopy =
    recentJobs.length === 0
      ? "まだ実行履歴がないため、最初のランでキューと品質が立ち上がります。"
      : `${recentJobs.length}件の最新ジョブを右レールで切り替えできます。`;
  const themeCopy =
    themeMode === "dark" ? "deep sea dark" : "mist blue light";
  const galleryTimeline = galleryItems.slice(0, 12);
  const installedModelCount =
    availableImageModels + availableAudioModels + availableVideoModels;
  const totalModelCount =
    (imageModels.length || 1) + (audioModels.length || 1) + (videoModels.length || 1);
  const activeModelDisplayName =
    mediaType === "image"
      ? selectedImageModel?.displayName ?? defaultImageModelId
      : mediaType === "audio"
        ? selectedAudioModel?.displayName ?? defaultAudioModelId
        : selectedVideoModel?.displayName ?? defaultVideoModelId;
  const composerHeading =
    mediaType === "image"
      ? "Prompt, style, and render settings"
      : mediaType === "audio"
        ? "Prompt, mood, tempo, and loop settings"
        : "Prompt, shot intent, and motion settings";
  const composerFormId = `composer-form-${mediaType}`;

  return (
    <main
      className={`app-shell app-shell--studio ${
        isComposerCollapsed ? "app-shell--composer-collapsed" : ""
      }`}
    >
      <aside className="studio-sidebar">
        <article className="section-card section-card--sidebar">
          <div className="sidebar-brand">
            <p className="eyebrow">Creative AI Studio</p>
            <h1>Make locally.</h1>
            <p className="sidebar-copy">
              Image, music, and storyboard generation with a fixed composer and a
              chronological gallery.
            </p>
            <p className="sidebar-meta">
              Live {liveSurfaceCount} / Roadmap {roadmapSurfaceCount}
            </p>
          </div>

          <div className="theme-switch" aria-label="Theme switcher">
            <span className="theme-switch__label">{themeCopy}</span>
            <div className="theme-switch__group" role="group" aria-label="Theme mode">
              <button
                type="button"
                className={`theme-switch__button ${
                  themeMode === "light" ? "is-active" : ""
                }`}
                onClick={() => setThemeMode("light")}
                aria-pressed={themeMode === "light"}
              >
                Light
              </button>
              <button
                type="button"
                className={`theme-switch__button ${
                  themeMode === "dark" ? "is-active" : ""
                }`}
                onClick={() => setThemeMode("dark")}
                aria-pressed={themeMode === "dark"}
              >
                Dark
              </button>
            </div>
          </div>
        </article>

        <article className="section-card section-card--nav">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Navigation</p>
              <h2>Move by surface</h2>
            </div>
          </div>
          <div className="surface-nav" aria-label="Creative surfaces">
            {creativeSurfaces.map((surface) => {
              const isActive = surface.mediaType === mediaType;
              const isClickable = surface.status === "live" && surface.mediaType;
              return (
                <button
                  key={surface.id}
                  type="button"
                  className={`surface-nav__item ${isActive ? "is-active" : ""} ${
                    surface.status === "soon" ? "is-soon" : ""
                  }`}
                  disabled={!isClickable}
                  onClick={() => {
                    if (surface.mediaType) {
                      setJob(null);
                      setMediaType(surface.mediaType);
                    }
                  }}
                >
                  <div className="surface-nav__topline">
                    <span className="mode-card__label">{surface.label}</span>
                    <span className={`surface-state surface-state--${surface.status}`}>
                      {surface.status}
                    </span>
                  </div>
                  <strong>{surface.description}</strong>
                </button>
              );
            })}
          </div>
        </article>

        <article className="section-card section-card--models">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Model Shelf</p>
              <h2>Installed and ready</h2>
            </div>
          </div>
          <div className="model-shelf">
            <div className="model-shelf__item">
              <span className="note-item__label">Image</span>
              <strong>{selectedImageModel?.displayName ?? defaultImageModelId}</strong>
              <p>{availableImageModels}/{imageModels.length || 1} installed</p>
            </div>
            <div className="model-shelf__item">
              <span className="note-item__label">Audio</span>
              <strong>{selectedAudioModel?.displayName ?? defaultAudioModelId}</strong>
              <p>{availableAudioModels}/{audioModels.length || 1} installed</p>
            </div>
            <div className="model-shelf__item">
              <span className="note-item__label">Video</span>
              <strong>{selectedVideoModel?.displayName ?? defaultVideoModelId}</strong>
              <p>{availableVideoModels}/{videoModels.length || 1} installed</p>
            </div>
          </div>
          <p className="section-footnote">
            Missing models can now be opened directly from the composer install guides.
          </p>
        </article>
      </aside>

      <div className="studio-main">
        <section className="workspace-bar" aria-label="Studio metrics">
          <article className="metric-pill">
            <span className="metric-label">Active Surface</span>
            <strong>{activeSurfaceLabel}</strong>
            <p>{activeSurfaceSummary}</p>
          </article>
          <article className="metric-pill">
            <span className="metric-label">Run State</span>
            <strong>{isSubmitting ? "rendering" : canSubmit ? "ready" : "awaiting model"}</strong>
            <p>{selectionStatusMessage}</p>
          </article>
          <article className="metric-pill">
            <span className="metric-label">Queue Load</span>
            <strong>{runningJobs}</strong>
            <p>queued, preparing, running</p>
          </article>
          <article className="metric-pill">
            <span className="metric-label">Studio Success</span>
            <strong>{formatPercent(metricsSummary?.success_rate)}</strong>
            <p>recent {formatPercent(metricsSummary?.recent_success_rate)}</p>
          </article>
        </section>

        <section className="workspace-grid">
          <div className="stage-stack">
            <article className="section-card section-card--stage">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">Preview</p>
                  <h2>
                    {isAudioStage
                      ? "Playback booth with the latest save and job status"
                      : isVideoStage
                        ? "Storyboard reel with the latest save and job status"
                        : "Preview canvas with the latest save and job status"}
                  </h2>
                </div>
                <span
                  className={`status-chip status-chip--${
                    activeStageJob?.status ?? "idle"
                  }`}
                >
                  {activeStageJob?.status ?? "idle"}
                </span>
              </div>

              {requestError ? <p className="error-banner">{requestError}</p> : null}

              <div
                className={`stage-surface stage-surface--hero ${
                  isAudioStage ? "stage-surface--audio" : ""
                }`}
              >
                {activeStageJob ? (
                  <div className={`stage-progress ${isStageJobInFlight ? "is-live" : ""}`}>
                    <div className="stage-progress__topline">
                      <span>
                        {isStageJobInFlight
                          ? "Generation Progress"
                          : activeStageJob.status === "succeeded"
                            ? "Generation Complete"
                            : "Last Generation State"}
                      </span>
                      <strong>{stageProgressPercent}%</strong>
                    </div>
                    <div
                      className={`stage-progress__track ${
                        activeStageJob.status === "failed" ||
                        activeStageJob.status === "cancelled"
                          ? "is-failed"
                          : ""
                      }`}
                    >
                      <span style={{ width: `${stageProgressPercent}%` }} />
                    </div>
                  </div>
                ) : null}
                {outputUrl && !isAudioStage ? (
                  <img src={outputUrl} alt="Generated output preview" />
                ) : null}
                {outputUrl && isAudioStage ? (
                  <div className="audio-preview">
                    <div className="audio-preview__header">
                      <p className="eyebrow">Playback</p>
                      <strong>{summarizePrompt(activeStageJob?.request.prompt ?? "", "New loop")}</strong>
                    </div>
                    <audio controls preload="metadata" src={outputUrl}>
                      Your browser does not support audio playback.
                    </audio>
                    <div className="audio-preview__facts">
                      {audioStageDuration ? <span>{audioStageDuration}</span> : null}
                      {audioStageBpm ? <span>{audioStageBpm}</span> : null}
                      {audioStageMood ? <span>{audioStageMood}</span> : null}
                    </div>
                  </div>
                ) : null}
                {outputUrl ? (
                  <div className="stage-overlay">
                    <div className="stage-overlay__chip">
                      <span className="metric-label">Focused Job</span>
                      <strong>{activeStageJob?.id ?? "-"}</strong>
                    </div>
                    <div className="stage-overlay__chip">
                      <span className="metric-label">Creative Blend</span>
                      <strong>{formatScore(qualityReport?.creative_alignment_score)}</strong>
                    </div>
                  </div>
                ) : (
                  <div className="empty-stage">
                    <p className="eyebrow">Awaiting Output</p>
                    <h3>
                      {mediaType === "image"
                        ? "Push an image run to inspect the canvas."
                        : mediaType === "audio"
                          ? "Push a song run to inspect the booth."
                          : "Push a video run to inspect the storyboard reel."}
                    </h3>
                    <p>
                      SeaArt 系の制作フローに合わせて、設定とキャンバスを分離しています。成果物が出ると
                      品質、semantic、保存状態、履歴が同時に更新されます。
                    </p>
                  </div>
                )}
              </div>

              <div className="job-meta-grid">
                <div className="job-meta-card">
                  <span>Job ID</span>
                  <strong>{activeStageJob?.id ?? "-"}</strong>
                </div>
                <div className="job-meta-card">
                  <span>Media</span>
                  <strong>{activeStageJob?.media_type ?? mediaType}</strong>
                </div>
                <div className="job-meta-card">
                  <span>Progress</span>
                  <strong>
                    {activeStageJob ? `${Math.round(activeStageJob.progress * 100)}%` : "0%"}
                  </strong>
                </div>
                <div className="job-meta-card">
                  <span>Updated</span>
                  <strong>{activeStageJob ? formatTimestamp(activeStageJob.updated_at) : "-"}</strong>
                </div>
              </div>

              {outputPath ? (
                <div className="asset-path">
                  <span>Asset Path</span>
                  <code>{outputPath}</code>
                </div>
              ) : null}
            </article>

            <article className="section-card section-card--analysis">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">Inspector</p>
                  <h2>Quality, semantic review, and metadata snapshot</h2>
                </div>
              </div>

              <div className="quality-grid">
                <div className="quality-card">
                  <span>Quality Score</span>
                  <strong>{formatScore(qualityReport?.quality_score)}</strong>
                  <p>{qualityReport?.quality_level ?? "not scored"}</p>
                </div>
                <div className="quality-card">
                  <span>Business Readiness</span>
                  <strong>{formatScore(qualityReport?.business_readiness_score)}</strong>
                  <p>{qualityReport?.business_readiness_level ?? "not scored"}</p>
                </div>
                <div className="quality-card">
                  <span>Semantic Alignment</span>
                  <strong>{formatScore(qualityReport?.semantic_alignment_score)}</strong>
                  <p>{qualityReport?.semantic_alignment_level ?? semanticReportStatus}</p>
                </div>
                <div className="quality-card">
                  <span>Creative Blend</span>
                  <strong>{formatScore(qualityReport?.creative_alignment_score)}</strong>
                  <p>{qualityReport?.creative_alignment_level ?? "not scored"}</p>
                </div>
                <div className="quality-card">
                  <span>Save Integrity</span>
                  <strong>{outputPath ? "saved" : "-"}</strong>
                  <p>{outputPath ? "output file path resolved" : "no output saved yet"}</p>
                </div>
                <div className="quality-card">
                  <span>Semantic Judge</span>
                  <strong>{semanticReportStatus}</strong>
                  <p>{semanticReportReason ?? "local judge model status"}</p>
                </div>
              </div>

              {qualityReport?.checks && qualityReport.checks.length > 0 ? (
                <div className="quality-check-list">
                  {qualityReport.checks.map((check) => (
                    <div key={check} className="quality-check-item">
                      {check}
                    </div>
                  ))}
                </div>
              ) : null}

              {qualityMetricsEntries.length > 0 ? (
                <div className="metadata-grid">
                  {qualityMetricsEntries.map(([key, value]) => (
                    <div key={key} className="metadata-item">
                      <span>{key}</span>
                      <strong>{stringifyValue(value)}</strong>
                    </div>
                  ))}
                </div>
              ) : null}

              {metadataEntries.length > 0 ? (
                <div className="metadata-grid">
                  {metadataEntries.map(([key, value]) => (
                    <div key={key} className="metadata-item">
                      <span>{key}</span>
                      <strong>{stringifyValue(value)}</strong>
                    </div>
                  ))}
                </div>
              ) : null}
            </article>
          </div>

          <aside className="timeline-stack">
            <article className="section-card section-card--monitor">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">Studio Pulse</p>
                  <h2>Keep the current mode and queue in view</h2>
                </div>
              </div>
              <div className="monitor-stack">
                <div className="monitor-stat">
                  <span className="note-item__label">Installed Models</span>
                  <strong>
                    {installedModelCount}/{totalModelCount}
                  </strong>
                  <p>{activeModelDisplayName}</p>
                </div>
                <div className="monitor-stat">
                  <span className="note-item__label">Recent Success</span>
                  <strong>{formatPercent(metricsSummary?.recent_success_rate)}</strong>
                  <p>{metricsSummary?.recent_window_size ?? 0} runs window</p>
                </div>
                <div className="monitor-stat">
                  <span className="note-item__label">Recent Avg Quality</span>
                  <strong>{formatScore(metricsSummary?.recent_average_quality_score)}</strong>
                  <p>technical proxy across latest jobs</p>
                </div>
                <div className="monitor-stat">
                  <span className="note-item__label">Media Focus</span>
                  <strong>
                    {mediaType === "image"
                      ? "visual pipeline"
                      : mediaType === "audio"
                        ? "audio pipeline"
                        : "motion pipeline"}
                  </strong>
                  <p>{activityWindowCopy}</p>
                </div>
              </div>
            </article>

            <article className="section-card section-card--gallery">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">Creation Timeline</p>
                  <h2>Newest outputs first</h2>
                </div>
              </div>

              <div className="gallery-list">
                {galleryTimeline.length === 0 ? (
                  <div className="history-empty">
                    まだ成果物がありません。最初の image / music / video ジョブを実行してください。
                  </div>
                ) : (
                  galleryTimeline.map((item) => {
                    const assetPath = item.preview_path ?? item.output_path;
                    const assetUrl = toAssetUrl(assetPath);
                    const isAudioItem = item.media_type === "audio";
                    return (
                      <button
                        key={item.job_id}
                        type="button"
                        className={`gallery-item ${
                          activeStageJob?.id === item.job_id ? "is-active" : ""
                        }`}
                        onClick={() => {
                          void focusJob(item.job_id, item.media_type);
                        }}
                      >
                        <div className={`gallery-item__thumb ${isAudioItem ? "is-audio" : ""}`}>
                          {isAudioItem ? (
                            <div className="gallery-item__audio-badge">
                              <span>Audio</span>
                            </div>
                          ) : (
                            <img
                              src={assetUrl}
                              alt={summarizePrompt(item.prompt, "Generated content")}
                            />
                          )}
                        </div>
                        <div className="gallery-item__body">
                          <div className="gallery-item__topline">
                            <span className="history-item__media">{item.media_type}</span>
                            <span className="history-score">
                              Q {formatScore(item.quality_score)}
                            </span>
                          </div>
                          <strong>
                            {summarizePrompt(
                              item.prompt,
                              item.media_type === "image"
                                ? "Untitled visual prompt"
                                : item.media_type === "audio"
                                  ? "Untitled music prompt"
                                  : "Untitled storyboard prompt",
                            )}
                          </strong>
                          <div className="history-item__meta">
                            <p>{formatTimestamp(item.created_at)}</p>
                            <span className={`status-chip status-chip--${item.success ? "succeeded" : "failed"}`}>
                              {item.success ? "saved" : "failed"}
                            </span>
                          </div>
                        </div>
                      </button>
                    );
                  })
                )}
              </div>
            </article>
          </aside>
        </section>

        <section className="detail-grid">
            <article className="section-card section-card--brief">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">Reference Shelf</p>
                  <h2>Current stack, roadmap, and quality framing</h2>
              </div>
            </div>
              <div className="brief-grid">
              <div className="brief-card">
                <span className="note-item__label">Image Stack</span>
                <strong>{selectedImageModel?.displayName ?? defaultImageModelId}</strong>
                <p>
                  {selectedImageModel?.tags.length
                    ? selectedImageModel.tags.join(" / ")
                    : "manifest-backed local image runtime"}
                </p>
              </div>
              <div className="brief-card">
                <span className="note-item__label">Audio Stack</span>
                <strong>{selectedAudioModel?.displayName ?? defaultAudioModelId}</strong>
                <p>
                  {selectedAudioModel?.tags.length
                    ? selectedAudioModel.tags.join(" / ")
                    : "manifest-backed local audio runtime"}
                </p>
              </div>
              <div className="brief-card">
                <span className="note-item__label">Video Stack</span>
                <strong>{selectedVideoModel?.displayName ?? defaultVideoModelId}</strong>
                <p>
                  {selectedVideoModel?.tags.length
                    ? selectedVideoModel.tags.join(" / ")
                    : "local procedural storyboard runtime"}
                </p>
              </div>
              <div className="brief-card">
                <span className="note-item__label">Quality Method</span>
                <strong>{qualityReport?.method ?? "heuristic_local_v1"}</strong>
                <p>technical proxy first, semantic judge optional, final taste review by human</p>
              </div>
                <div className="brief-card">
                  <span className="note-item__label">Mode Health</span>
                  <strong>{formatPercent(selectedMediaMetrics?.success_rate)}</strong>
                  <p>
                    save {formatPercent(selectedMediaMetrics?.save_success_rate)} / semantic{" "}
                    {formatScore(selectedMediaMetrics?.average_semantic_alignment_score)}
                  </p>
                </div>
                <div className="brief-card">
                  <span className="note-item__label">Comic Workflow</span>
                  <strong>Roadmap</strong>
                  <p>
                    multi-panel image sequencing, page layout, and dialogue staging after image
                    controls settle.
                  </p>
                </div>
              </div>
            </article>

          <article className="section-card section-card--snapshot">
            <div className="section-card__header">
              <div>
                <p className="eyebrow">Payload Mirror</p>
                <h2>Current request, job, and studio metrics</h2>
              </div>
            </div>
            <pre>
              {JSON.stringify(
                {
                  request: requestSnapshot,
                  job: activeStageJob,
                  metrics: metricsSummary,
                },
                null,
                2,
              )}
            </pre>
          </article>
        </section>
      </div>

      <section
        className={`composer-dock ${isComposerCollapsed ? "is-collapsed" : ""}`}
        aria-label="Composer dock"
      >
        <div className="composer-dock__frame">
          <div className="composer-dock__header">
            <div>
              <p className="eyebrow">Composer Dock</p>
              <h2>{composerHeading}</h2>
            </div>
            <div className="composer-dock__header-actions">
              <div className="composer-dock__status">
                <span className="surface-pill">{activeSurfaceLabel}</span>
                <span className={`status-chip status-chip--${isSubmitting ? "running" : "idle"}`}>
                  {isSubmitting ? "Generating" : canSubmit ? "Ready" : "Hold"}
                </span>
              </div>
              <button
                type="submit"
                form={composerFormId}
                className="dock-submit"
                disabled={isSubmitting || !canSubmit || isComposerCollapsed}
              >
                {isSubmitting
                  ? "Generating..."
                  : canSubmit
                    ? "Generate"
                    : "Install a model first"}
              </button>
              <button
                type="button"
                className="dock-toggle"
                onClick={() => setIsComposerCollapsed((current) => !current)}
                aria-expanded={!isComposerCollapsed}
              >
                {isComposerCollapsed ? "Show Controls" : "Hide Controls"}
              </button>
            </div>
          </div>
          <div className="composer-dock__meta">
            <span>{activeModelDisplayName}</span>
            <span>{selectionStatusMessage}</span>
            <span>
              {activeStageJob ? `${stageProgressPercent}% ${activeStageJob.status}` : "No active job"}
            </span>
          </div>
          {!isComposerCollapsed ? (
            <div className="composer-dock__body">
              <PromptForm
                formId={composerFormId}
                key={`${mediaType}:${
                  mediaType === "image"
                    ? defaultImageModelId
                    : mediaType === "audio"
                      ? defaultAudioModelId
                      : defaultVideoModelId
                }`}
                mediaType={mediaType}
                modelOptions={
                  mediaType === "image"
                    ? imageModels
                    : mediaType === "audio"
                      ? audioModels
                      : videoModels
                }
                loraOptions={mediaType === "image" ? loraOptions : []}
                initialValues={{
                  ...drafts[mediaType],
                  modelId:
                    mediaType === "image"
                      ? selectedImageModelId
                      : mediaType === "audio"
                        ? selectedAudioModelId
                        : selectedVideoModelId,
                }}
                disabled={isSubmitting}
                canSubmit={canSubmit}
                statusMessage={selectionStatusMessage}
                submitLabel={
                  isSubmitting
                    ? "Generating..."
                    : canSubmit
                      ? "Generate"
                      : "Install a model first"
                }
                onDraftChange={(nextDraft) => {
                  setDrafts((current) => ({
                    ...current,
                    [mediaType]: {
                      ...current[mediaType],
                      ...nextDraft,
                    },
                  }));
                }}
                onSubmit={(values) => {
                  void submitGeneration(values);
                }}
              />
            </div>
          ) : null}
        </div>
      </section>
    </main>
  );
}
