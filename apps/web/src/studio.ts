import type {
  LoraOption,
  MediaType,
  ModelOption,
  PromptFormSubmitValues,
} from "./components/promptFormTypes";

export type { LoraOption, MediaType, ModelOption, PromptFormSubmitValues };

export type JobStatus =
  | "queued"
  | "preparing"
  | "running"
  | "postprocessing"
  | "succeeded"
  | "failed"
  | "cancelled";

export type ThemeMode = "light" | "dark";

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
  availability_reason: string | null;
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
  semantic_status: string | null;
  semantic_backend: string | null;
  semantic_reason: string | null;
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

export type MediaMetrics = {
  total_jobs: number;
  success_rate: number;
  average_quality_score: number | null;
  average_semantic_alignment_score: number | null;
  average_creative_alignment_score: number | null;
  semantic_disabled_jobs?: number;
  feedback_total: number;
  feedback_coverage_rate: number;
};

export type ProjectResponse = {
  id: string;
  name: string;
  description: string;
  status: string;
  tags: string[];
  metadata: Record<string, unknown>;
  pinned_asset_ids: string[];
  created_at: string;
  updated_at: string;
  job_ids: string[];
  job_count: number;
  asset_count: number;
  cover_asset_path: string | null;
};

export type ProjectAssetResponse = {
  asset_id: string;
  job_id: string;
  media_type: MediaType;
  prompt: string;
  output_path: string;
  preview_path: string | null;
  quality_score: number | null;
  quality_score_calibrated: number | null;
  semantic_alignment_score: number | null;
  creative_alignment_score: number | null;
  semantic_status: string | null;
  semantic_backend: string | null;
  semantic_reason: string | null;
  created_at: string;
  updated_at: string;
};

export type FeedbackResponse = {
  id: string;
  job_id: string;
  asset_id: string | null;
  project_id: string | null;
  quality_rating: number;
  semantic_rating: number | null;
  creative_rating: number | null;
  reuse_intent: boolean | null;
  export_ready: boolean | null;
  issue_tags: string[];
  comments: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type CreateFeedbackPayload = {
  job_id: string;
  asset_id: string | null;
  project_id: string | null;
  quality_rating: number;
  semantic_rating: number | null;
  creative_rating: number | null;
  reuse_intent: boolean | null;
  export_ready: boolean | null;
  issue_tags: string[];
  comments: string;
  metadata: Record<string, unknown>;
};

export type FeedbackFormValues = {
  qualityRating: number;
  semanticRating: number;
  creativeRating: number;
  reuseIntent: boolean;
  exportReady: boolean;
  issueTags: string[];
  comments: string;
};

export type ProjectJobsResponse = {
  project: ProjectResponse;
  jobs: JobResponse[];
  assets: ProjectAssetResponse[];
  job_count: number;
  asset_count: number;
  media_breakdown: Record<string, number>;
  average_quality_score: number | null;
  average_creative_alignment_score: number | null;
};

export type ExportProjectResponse = {
  project_id: string;
  bundle_root: string;
  manifest_path: string;
};

export type MetadataEntry = {
  id: string;
  key: string;
  value: string;
};

export type ProjectFormValues = {
  name: string;
  description: string;
  status: string;
  tagsText: string;
  metadataEntries: MetadataEntry[];
};

export type RefreshStudioOptions = {
  preferredAssetId?: string | null;
  preferredJobId?: string | null;
};

export const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";

export const terminalStatuses = new Set<JobStatus>(["succeeded", "failed", "cancelled"]);

export const defaultSubmitValues: Record<MediaType, PromptFormSubmitValues> = {
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

export const mediaTypeLabels: Record<MediaType, string> = {
  image: "画像",
  audio: "音声",
  video: "動画",
};

export const jobStatusLabels: Record<JobStatus, string> = {
  queued: "受付済み",
  preparing: "準備中",
  running: "生成中",
  postprocessing: "仕上げ中",
  succeeded: "完了",
  failed: "失敗",
  cancelled: "キャンセル",
};

export function normalizeModelOption(item: ModelSummary): ModelOption {
  return {
    id: item.id,
    displayName: item.display_name,
    defaultParams: item.default_params,
    tags: item.tags,
    isAvailable: item.is_available,
    isDefault: item.is_default,
    availabilityReason: item.availability_reason,
  };
}

export function normalizeLoraOption(item: LoraCatalogResponse["items"][number]): LoraOption {
  return {
    id: item.id,
    displayName: item.display_name,
    path: item.path,
    relativePath: item.relative_path,
  };
}

export function createOutputUrl(pathValue: string | null | undefined): string | null {
  if (!pathValue) {
    return null;
  }

  const normalized = pathValue.replace(/\\/g, "/");

  // Look for "outputs/" as a path segment
  const match = normalized.match(/(?:^|\/)outputs\//);
  if (!match) {
    return null;
  }

  // Slice from the start of "outputs/" (skipping optional leading slash in match)
  const startIndex = match.index! + (match[0].startsWith("/") ? 1 : 0);
  const subPath = normalized.slice(startIndex);

  return `${API_BASE_URL}/${subPath}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "未設定";
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) {
    return value;
  }

  return parsed.toLocaleString("ja-JP");
}

export function formatScore(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(1) : "未評価";
}

export function formatPercent(value: number | null | undefined): string {
  return typeof value === "number" ? `${value.toFixed(1)}%` : "未集計";
}

export function formatJobStatus(status: JobStatus): string {
  return jobStatusLabels[status];
}

export function isAudioAsset(pathValue: string | null | undefined): boolean {
  return Boolean(pathValue && /\.(wav|mp3|ogg|m4a)$/i.test(pathValue));
}

export function isVideoAsset(pathValue: string | null | undefined): boolean {
  return Boolean(pathValue && /\.(mp4|webm|mov)$/i.test(pathValue));
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

export function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function formatSemanticStatus(value: string | null | undefined): string {
  switch (value) {
    case "scored":
      return "評価済み";
    case "disabled":
      return "無効";
    case "unavailable":
      return "未利用";
    default:
      return "未設定";
  }
}

export function extractQualityReport(
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

export function createEmptyFeedbackFormValues(): FeedbackFormValues {
  return {
    qualityRating: 3,
    semanticRating: 3,
    creativeRating: 3,
    reuseIntent: false,
    exportReady: false,
    issueTags: [],
    comments: "",
  };
}

export function buildFeedbackPayload(
  asset: GalleryAssetDetailResponse,
  values: FeedbackFormValues,
): CreateFeedbackPayload {
  return {
    job_id: asset.job_id,
    asset_id: asset.asset_id,
    project_id: asset.project_id,
    quality_rating: values.qualityRating,
    semantic_rating: values.semanticRating,
    creative_rating: values.creativeRating,
    reuse_intent: values.reuseIntent,
    export_ready: values.exportReady,
    issue_tags: values.issueTags,
    comments: values.comments.trim(),
    metadata: {
      semantic_status: asset.semantic_status,
      semantic_backend: asset.semantic_backend,
    },
  };
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

export function buildGeneratePayload(
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

export function buildReusePayload(
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

function makeLocalId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export function createMetadataEntry(
  key = "",
  value = "",
): MetadataEntry {
  return {
    id: makeLocalId(),
    key,
    value,
  };
}

export function createEmptyProjectFormValues(): ProjectFormValues {
  return {
    name: "",
    description: "",
    status: "active",
    tagsText: "",
    metadataEntries: [],
  };
}

export function serializeProjectFormValues(values: ProjectFormValues): string {
  return JSON.stringify({
    name: values.name.trim(),
    description: values.description.trim(),
    status: values.status.trim() || "active",
    tags: parseTagText(values.tagsText),
    metadata: metadataEntriesToRecord(values.metadataEntries),
  });
}

export function areProjectFormValuesEqual(
  left: ProjectFormValues,
  right: ProjectFormValues,
): boolean {
  return serializeProjectFormValues(left) === serializeProjectFormValues(right);
}

export function projectToFormValues(project: ProjectResponse | null | undefined): ProjectFormValues {
  if (!project) {
    return createEmptyProjectFormValues();
  }

  const metadataEntries = Object.entries(project.metadata ?? {}).map(([key, value]) =>
    createMetadataEntry(key, value == null ? "" : String(value)),
  );

  return {
    name: project.name,
    description: project.description,
    status: project.status,
    tagsText: project.tags.join(", "),
    metadataEntries,
  };
}

export function parseTagText(value: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const rawTag of value.split(",")) {
    const tag = rawTag.trim();
    const normalized = tag.toLowerCase();
    if (!tag || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    tags.push(tag);
  }
  return tags;
}

export function metadataEntriesToRecord(entries: MetadataEntry[]): Record<string, string> {
  const record: Record<string, string> = {};
  for (const entry of entries) {
    const key = entry.key.trim();
    if (!key) {
      continue;
    }
    record[key] = entry.value.trim();
  }
  return record;
}

export function buildProjectPayload(values: ProjectFormValues): Record<string, unknown> {
  return {
    name: values.name.trim(),
    description: values.description.trim(),
    status: values.status.trim() || "active",
    tags: parseTagText(values.tagsText),
    metadata: metadataEntriesToRecord(values.metadataEntries),
  };
}
