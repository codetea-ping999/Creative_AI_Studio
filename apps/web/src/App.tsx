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
  request: {
    prompt: string;
    negative_prompt: string | null;
    model_id: string;
    seed: number | null;
    params: Record<string, unknown>;
  };
  result: {
    outputs: string[];
    previews: string[];
    metadata: Record<string, unknown>;
  } | null;
  created_at: string;
  updated_at: string;
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

const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";

const terminalStatuses = new Set<JobStatus>(["succeeded", "failed", "cancelled"]);

const draftDefaults: Record<MediaType, Partial<PromptFormSubmitValues>> = {
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
  const [modelOptionsByMedia, setModelOptionsByMedia] = useState<
    Record<MediaType, ModelOption[]>
  >({
    image: [],
    audio: [],
    video: [],
  });
  const [loraOptions, setLoraOptions] = useState<LoraOption[]>([]);
  const [drafts, setDrafts] =
    useState<Record<MediaType, Partial<PromptFormSubmitValues>>>(draftDefaults);
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [galleryItems, setGalleryItems] = useState<GalleryItemResponse[]>([]);
  const [metrics, setMetrics] = useState<MetricsSummaryResponse | null>(null);
  const [latestJob, setLatestJob] = useState<JobResponse | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
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
          const currentDraft = current[targetMediaType] ?? {};
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

  async function refreshStudio(targetMediaType: MediaType): Promise<void> {
    try {
      const [galleryPayload, metricsPayload] = await Promise.all([
        requestJson<GalleryItemResponse[]>(
          `/gallery?media_type=${encodeURIComponent(targetMediaType)}&limit=8`,
        ),
        requestJson<MetricsSummaryResponse>("/metrics/summary"),
      ]);

      startTransition(() => {
        setGalleryItems(galleryPayload);
        setMetrics(metricsPayload);
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
          void refreshStudio(payload.media_type);
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

    const payload = buildGeneratePayload(values, selectedProjectId || null);

    try {
      const created = await requestJson<CreateJobResponse>(`/generate/${values.mediaType}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
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

  return (
    <div className="app-shell app-shell--studio">
      <aside className="studio-sidebar">
        <section className="section-card section-card--nav">
          <div className="sidebar-brand">
            <p className="eyebrow">Creative AI Studio</p>
            <h1>Local generation cockpit</h1>
            <p className="sidebar-copy">
              Queue image, audio, and video jobs against the local API and watch the latest
              outputs land in one place.
            </p>
          </div>
          <div className="theme-switch">
            <span className="theme-switch__label">Theme</span>
            <button
              type="button"
              onClick={() =>
                setThemeMode((current) => (current === "light" ? "dark" : "light"))
              }
            >
              {themeMode === "light" ? "Dark" : "Light"}
            </button>
          </div>
        </section>

        <section className="section-card">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Surface</p>
              <h2>Choose a media lane</h2>
            </div>
          </div>
          <div className="workspace-bar">
            {(["image", "audio", "video"] as MediaType[]).map((option) => (
              <button
                key={option}
                type="button"
                className="metric-pill"
                onClick={() => setMediaType(option)}
                aria-pressed={mediaType === option}
              >
                <strong>{mediaTypeLabels[option]}</strong>
                <p>{option === mediaType ? "Active" : "Switch"}</p>
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
            <span>Project</span>
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
              ? `${projects.length} projects available for direct job binding.`
              : "Create a project through the API to bind jobs from the UI."}
          </p>
        </section>
      </aside>

      <main className="studio-main">
        <section className="section-card section-card--stage">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">Composer</p>
              <h2>{mediaTypeLabels[mediaType]} generation</h2>
            </div>
            <p className="section-footnote">
              Model manifests are loaded live from the local registry.
            </p>
          </div>
          <PromptForm
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
          {errorMessage ? <p className="section-footnote">{errorMessage}</p> : null}
        </section>

        <div className="workspace-grid">
          <section className="section-card section-card--monitor">
            <div className="section-card__header">
              <div>
                <p className="eyebrow">Latest Job</p>
                <h2>Run state and outputs</h2>
              </div>
            </div>
            {latestJob ? (
              <div className="monitor-stack">
                <div className="metric-pill">
                  <strong>{latestJob.status}</strong>
                  <p>{latestJob.id}</p>
                </div>
                <div className="metadata-grid">
                  <div className="metadata-item">
                    <span>Prompt</span>
                    <p>{latestJob.request.prompt}</p>
                  </div>
                  <div className="metadata-item">
                    <span>Model</span>
                    <p>{latestJob.request.model_id || "default"}</p>
                  </div>
                  <div className="metadata-item">
                    <span>Updated</span>
                    <p>{formatDate(latestJob.updated_at)}</p>
                  </div>
                  <div className="metadata-item">
                    <span>Quality</span>
                    <p>
                      {formatScore(
                        Number(
                          latestJob.result?.metadata.quality_report &&
                            typeof latestJob.result.metadata.quality_report === "object" &&
                            "quality_score" in latestJob.result.metadata.quality_report
                            ? (latestJob.result.metadata.quality_report as Record<string, unknown>)
                                .quality_score
                            : null,
                        ),
                      )}
                    </p>
                  </div>
                </div>
                <OutputPreview
                  mediaType={latestJob.media_type}
                  outputPath={latestJob.result?.previews[0] ?? latestJob.result?.outputs[0] ?? null}
                />
                {latestJob.error_message ? (
                  <p className="section-footnote">{latestJob.error_message}</p>
                ) : null}
              </div>
            ) : (
              <div className="empty-stage">
                <h3>No job selected</h3>
                <p>Queue a generation to inspect runtime status and the latest output preview.</p>
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
                  <article key={item.asset_id} className="section-card">
                    <div className="section-card__header">
                      <div>
                        <p className="eyebrow">{item.project_name || "Unassigned"}</p>
                        <h2>{item.prompt}</h2>
                      </div>
                    </div>
                    <OutputPreview mediaType={item.media_type} outputPath={item.preview_path} />
                    <div className="metadata-grid">
                      <div className="metadata-item">
                        <span>Quality</span>
                        <p>{formatScore(item.quality_score_calibrated ?? item.quality_score)}</p>
                      </div>
                      <div className="metadata-item">
                        <span>Semantic</span>
                        <p>
                          {formatScore(
                            item.semantic_alignment_score_calibrated ??
                              item.semantic_alignment_score,
                          )}
                        </p>
                      </div>
                      <div className="metadata-item">
                        <span>Creative</span>
                        <p>
                          {formatScore(
                            item.creative_alignment_score_calibrated ??
                              item.creative_alignment_score,
                          )}
                        </p>
                      </div>
                      <div className="metadata-item">
                        <span>Feedback</span>
                        <p>{item.feedback_count}</p>
                      </div>
                    </div>
                  </article>
                ))
              ) : (
                <div className="empty-stage">
                  <h3>No assets yet</h3>
                  <p>Successful jobs will appear here after the runner finishes them.</p>
                </div>
              )}
            </div>
          </section>
        </div>

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

type OutputPreviewProps = {
  mediaType: MediaType;
  outputPath: string | null;
};

function OutputPreview({ mediaType, outputPath }: OutputPreviewProps) {
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="empty-stage">
        <h3>Preview unavailable</h3>
        <p>{outputPath ?? "No output path was returned by the API."}</p>
      </div>
    );
  }

  if (mediaType === "audio" || isAudioAsset(outputPath)) {
    return <audio controls preload="metadata" src={src} />;
  }

  if (isVideoAsset(outputPath)) {
    return <video controls muted playsInline preload="metadata" src={src} />;
  }

  return <img src={src} alt="Generated asset preview" loading="lazy" />;
}

export default App;
