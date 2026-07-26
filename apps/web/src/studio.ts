import type {
  LoraOption,
  MediaType,
  ModelOption,
  PromptFormSubmitValues,
} from "./components/promptFormTypes";

export type JobStatus =
  | "queued"
  | "preparing"
  | "running"
  | "postprocessing"
  | "succeeded"
  | "failed"
  | "cancelled";

export type CreateJobResponse = {
  job_id: string;
  status: JobStatus;
};

export type GenerationRequestSnapshot = {
  media_type: MediaType;
  prompt: string;
  negative_prompt: string | null;
  model_id: string;
  seed: number | null;
  output_format: string | null;
  params: Record<string, unknown>;
};

export type JobResponse = {
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

export type ModelSummary = {
  id: string;
  display_name: string;
  default_params: Record<string, unknown>;
  tags: string[];
  is_available: boolean;
  is_default: boolean;
  runtime_status?: string;
  availability_message?: string;
};

export type ModelsResponse = {
  models: ModelSummary[];
};

export type LoraCatalogResponse = {
  items: Array<{
    id: string;
    display_name: string;
    path: string;
    relative_path: string;
  }>;
};

export type GalleryItemResponse = {
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

export type GalleryAssetDetailResponse = GalleryItemResponse & {
  quality_report: Record<string, unknown>;
  request_snapshot: GenerationRequestSnapshot;
  metadata: Record<string, unknown>;
  feedback_summary: Record<string, unknown>;
  export_paths: string[];
  parent_asset_id: string | null;
  lineage: string[];
  tags: string[];
};

export type GalleryStatsResponse = {
  total_items: number;
  total_by_media_type: Record<string, number>;
  total_by_project: Record<string, number>;
  average_quality_score: number | null;
  total_reuse_count: number;
  total_export_count: number;
};

export type ReuseAssetResponse = {
  asset_id: string;
  job_id: string;
  status: JobStatus;
  project_id: string | null;
};

export type ExportAssetResponse = {
  asset_id: string;
  export_path: string;
  metadata_path: string | null;
};

export type MediaMetrics = {
  total_jobs: number;
  success_rate: number;
  average_quality_score: number | null;
  average_semantic_alignment_score: number | null;
  average_creative_alignment_score: number | null;
  feedback_total: number;
  feedback_coverage_rate: number;
};

export type MetricsSummaryResponse = {
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

export type ProjectResponse = {
  id: string;
  name: string;
  description: string;
  status: string;
  tags: string[];
  pinned_asset_ids: string[];
  asset_count: number;
  job_count: number;
  cover_asset_path: string | null;
};

export type FeedbackResponse = {
  id: string;
  quality_rating: number;
  semantic_rating: number | null;
  creative_rating: number | null;
};

export type RefreshStudioOptions = {
  preferredAssetId?: string | null;
  preferredJobId?: string | null;
};

export const terminalStatuses = new Set<JobStatus>([
  "succeeded",
  "failed",
  "cancelled",
]);

export const defaultSubmitValues: Record<MediaType, PromptFormSubmitValues> = {
  image: {
    mediaType: "image",
    modelId: "sdxl",
    outputFormat: "png",
    prompt: "",
    negativePrompt: "",
    imageBriefPurpose: "SNS投稿",
    imageBriefSubject: "",
    imageBriefMood: "やわらかい光",
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
    genre: "electronic",
    instruments: "warm analog synth, soft percussion",
    structure: "seamless loop",
    temperature: 1,
    topK: 250,
    topP: 0,
    cameraMotion: "push-in",
    visualStyle: "storyboard",
  },
  audio: {
    mediaType: "audio",
    modelId: "musicgen-small",
    outputFormat: "wav",
    prompt: "",
    negativePrompt: "",
    imageBriefPurpose: "SNS投稿",
    imageBriefSubject: "",
    imageBriefMood: "やわらかい光",
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
    genre: "electronic",
    instruments: "warm analog synth, soft percussion",
    structure: "seamless loop",
    temperature: 1,
    topK: 250,
    topP: 0,
    cameraMotion: "push-in",
    visualStyle: "storyboard",
  },
  video: {
    mediaType: "video",
    modelId: "storyboard-video",
    outputFormat: "gif",
    prompt: "",
    negativePrompt: "",
    imageBriefPurpose: "SNS投稿",
    imageBriefSubject: "",
    imageBriefMood: "やわらかい光",
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
    genre: "electronic",
    instruments: "warm analog synth, soft percussion",
    structure: "seamless loop",
    temperature: 1,
    topK: 250,
    topP: 0,
    cameraMotion: "push-in",
    visualStyle: "storyboard",
  },
};

export const mediaTypeLabels: Record<MediaType, string> = {
  image: "Image",
  audio: "Audio",
  video: "Video",
};

export function normalizeModelOption(item: ModelSummary): ModelOption {
  return {
    id: item.id,
    displayName: item.display_name,
    defaultParams: item.default_params,
    tags: item.tags,
    isAvailable: item.is_available,
    isDefault: item.is_default,
    runtimeStatus: item.runtime_status ?? (item.is_available ? "ready" : "missing_files"),
    availabilityMessage: item.availability_message ?? "",
  };
}

export function normalizeLoraOption(
  item: LoraCatalogResponse["items"][number],
): LoraOption {
  return {
    id: item.id,
    displayName: item.display_name,
    path: item.path,
    relativePath: item.relative_path,
  };
}

export function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "n/a";
  }

  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

export function formatScore(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(1) : "n/a";
}

export function formatPercent(value: number | null | undefined): string {
  return typeof value === "number" ? `${value.toFixed(1)}%` : "n/a";
}

export function isAudioAsset(pathValue: string | null | undefined): boolean {
  return Boolean(pathValue && /\.(wav|mp3|ogg|m4a)$/i.test(pathValue));
}

export function isVideoAsset(pathValue: string | null | undefined): boolean {
  return Boolean(pathValue && /\.(gif|mp4|webm|mov)$/i.test(pathValue));
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

export function extractJobQualityScore(job: JobResponse | null): number | null {
  return asNumber(extractQualityReport(job?.result?.metadata)?.quality_score);
}

export function mergeDraftWithDefaults(
  mediaType: MediaType,
  draft?: Partial<PromptFormSubmitValues>,
): PromptFormSubmitValues {
  return {
    ...defaultSubmitValues[mediaType],
    ...draft,
    mediaType,
  };
}

export function createDraftFromRequestSnapshot(
  request: GenerationRequestSnapshot,
): Partial<PromptFormSubmitValues> {
  const params = request.params ?? {};
  if (request.media_type === "image") {
    return {
      mediaType: "image",
      modelId: request.model_id,
      outputFormat: request.output_format ?? "png",
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
      outputFormat: request.output_format ?? "wav",
      prompt: request.prompt,
      durationSeconds:
        asNumber(params.duration_seconds) ?? defaultSubmitValues.audio.durationSeconds,
      guidanceScale:
        asNumber(params.guidance_scale) ?? defaultSubmitValues.audio.guidanceScale,
      bpm: asNumber(params.bpm) ?? defaultSubmitValues.audio.bpm,
      mood: asString(params.mood) ?? defaultSubmitValues.audio.mood,
      genre: asString(params.genre) ?? defaultSubmitValues.audio.genre,
      instruments:
        asString(params.instruments) ?? defaultSubmitValues.audio.instruments,
      structure: asString(params.structure) ?? defaultSubmitValues.audio.structure,
      temperature:
        asNumber(params.temperature) ?? defaultSubmitValues.audio.temperature,
      topK: asNumber(params.top_k) ?? defaultSubmitValues.audio.topK,
      topP: asNumber(params.top_p) ?? defaultSubmitValues.audio.topP,
      seed: request.seed,
    };
  }

  return {
    mediaType: "video",
    modelId: request.model_id,
    outputFormat: request.output_format ?? "gif",
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
