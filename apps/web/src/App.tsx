import { startTransition, useCallback, useEffect, useState, type FormEvent } from "react";
import {
  PromptForm,
  type LoraOption,
  type MediaType,
  type ModelOption,
  type PromptFormSubmitValues,
} from "./components/PromptForm";
import { AssetDetailPanel, type FeedbackFormValues } from "./components/AssetDetailPanel";
import { GalleryPanel } from "./components/GalleryPanel";
import { LatestJobPanel } from "./components/LatestJobPanel";
import { MatrixPanel } from "./components/MatrixPanel";
import { ModelsSummaryPanel } from "./components/ModelsSummaryPanel";
import { StoryPanel } from "./components/StoryPanel";
import { buildGeneratePayload, buildReusePayload } from "./lib/payloads";
import {
  buildQuickReviewPrompt,
  getQuickReviewIssueOptions,
  type QuickReviewIssueTag,
} from "./lib/quickReview";
import {
  createDraftFromRequestSnapshot,
  defaultSubmitValues,
  formatPercent,
  mediaTypeLabels,
  mergeDraftWithDefaults,
  normalizeLoraOption,
  normalizeModelOption,
  terminalStatuses,
  type CreateJobResponse,
  type ExportAssetResponse,
  type FeedbackResponse,
  type GalleryAssetDetailResponse,
  type GalleryItemResponse,
  type GalleryStatsResponse,
  type JobResponse,
  type LoraCatalogResponse,
  type MetricsSummaryResponse,
  type ModelsResponse,
  type ProjectResponse,
  type RefreshStudioOptions,
  type ReuseAssetResponse,
} from "./studio";
import { requestJson } from "./studioClient";

type ThemeMode = "light" | "dark";
type ModelLoadState = "idle" | "loading" | "loaded" | "error";

const mediaTypeReadinessLabels: Record<MediaType, string> = {
  image: "画像",
  audio: "音声",
  video: "動画",
};

type AssetReuseOptions = {
  action?: "rerun" | "variation";
  issueTags?: QuickReviewIssueTag[];
  sourceAsset?: GalleryAssetDetailResponse;
  useSourceSnapshot?: boolean;
};

function App() {
  const [themeMode, setThemeMode] = useState<ThemeMode>("dark");
  const [mediaType, setMediaType] = useState<MediaType>("image");
  const [composerRevision, setComposerRevision] = useState(0);
  const [modelOptionsByMedia, setModelOptionsByMedia] = useState<
    Record<MediaType, ModelOption[]>
  >({
    image: [],
    audio: [],
    video: [],
  });
  const [modelLoadState, setModelLoadState] = useState<Record<MediaType, ModelLoadState>>({
    image: "idle",
    audio: "idle",
    video: "idle",
  });
  const [loraOptions, setLoraOptions] = useState<LoraOption[]>([]);
  const [drafts, setDrafts] = useState<Record<MediaType, Partial<PromptFormSubmitValues>>>({
    image: defaultSubmitValues.image,
    audio: defaultSubmitValues.audio,
    video: defaultSubmitValues.video,
  });
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDescription, setNewProjectDescription] = useState("");
  const [projectStatusDraft, setProjectStatusDraft] = useState("active");
  const [projectTagsDraft, setProjectTagsDraft] = useState("");
  const [gallerySearch, setGallerySearch] = useState("");
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
  const [isProjectBusy, setIsProjectBusy] = useState(false);
  const [isFeedbackBusy, setIsFeedbackBusy] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [assetMessage, setAssetMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [apiReachable, setApiReachable] = useState<boolean | null>(null);

  const activeModels = modelOptionsByMedia[mediaType];
  const activeModelLoadState = modelLoadState[mediaType];
  const activeDraft = drafts[mediaType] ?? defaultSubmitValues[mediaType];
  const selectedModelAvailable = activeModels.some(
    (model) => model.id === activeDraft.modelId && model.isAvailable,
  );
  const activeAvailableModelCount = activeModels.filter((model) => model.isAvailable).length;
  const readinessState =
    apiReachable === null
      ? "checking"
      : apiReachable === false
        ? "offline"
        : activeModelLoadState === "idle" || activeModelLoadState === "loading"
          ? "models-loading"
          : activeModelLoadState === "error"
            ? "models-error"
            : activeAvailableModelCount > 0
              ? "ready"
              : "models-unavailable";
  const readinessCopy =
    readinessState === "ready"
      ? `${mediaTypeReadinessLabels[mediaType]}モデル ${activeAvailableModelCount} 件を利用できます。初回生成は準備に時間がかかる場合があります。`
      : readinessState === "models-loading"
        ? `${mediaTypeReadinessLabels[mediaType]}モデルを読み込んでいます。`
        : readinessState === "models-unavailable"
          ? `利用可能な${mediaTypeReadinessLabels[mediaType]}モデルがありません。モデルを配置してから再試行してください。`
          : readinessState === "models-error"
            ? `${mediaTypeReadinessLabels[mediaType]}モデルの一覧を読み込めません。再試行してください。`
            : readinessState === "offline"
              ? "API に接続できません。./scripts/start_studio.sh の起動状態を確認してください。"
              : "API とローカルモデルを確認しています。";
  const generationGateMessage =
    readinessState !== "ready"
      ? readinessCopy
      : !selectedModelAvailable
        ? "選択中のモデルは利用できません。利用可能なモデルを選択してください。"
        : null;
  const activeMetrics = metrics?.by_media[mediaType] ?? null;
  const selectedProject =
    projects.find((project) => project.id === selectedProjectId) ?? null;
  const handleDraftChange = useCallback(
    (nextDraft: Partial<PromptFormSubmitValues>) => {
      setDrafts((current) => {
        const currentDraft = current[mediaType];
        if (JSON.stringify(currentDraft) === JSON.stringify(nextDraft)) {
          return current;
        }

        return {
          ...current,
          [mediaType]: nextDraft,
        };
      });
    },
    [mediaType],
  );
  const loadDraftIntoComposer = useCallback(
    (targetMedia: MediaType, nextDraft: Partial<PromptFormSubmitValues>) => {
      setDrafts((current) => ({
        ...current,
        [targetMedia]: {
          ...current[targetMedia],
          ...nextDraft,
          mediaType: targetMedia,
        },
      }));
      setMediaType(targetMedia);
      setComposerRevision((current) => current + 1);
    },
    [],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = themeMode;
  }, [themeMode]);

  useEffect(() => {
    void loadReadiness();
    const timer = window.setInterval(() => {
      void loadReadiness();
    }, 10_000);
    return () => window.clearInterval(timer);
  }, []);

  async function loadReadiness(): Promise<boolean> {
    try {
      const payload = await requestJson<{ status: string }>("/health");
      const isReachable = payload.status === "ok";
      setApiReachable(isReachable);
      return isReachable;
    } catch {
      setApiReachable(false);
      return false;
    }
  }

  useEffect(() => {
    if (!apiReachable) {
      return;
    }
    void loadProjects();
    void loadLoras();
  }, [apiReachable]);

  useEffect(() => {
    if (!apiReachable) {
      return;
    }
    void loadModels(mediaType);
    void refreshStudio(mediaType);
  }, [apiReachable, mediaType]);

  useEffect(() => {
    if (!apiReachable) {
      return;
    }
    void refreshStudio(mediaType);
  }, [apiReachable, selectedProjectId, gallerySearch]);

  useEffect(() => {
    if (!activeJobId) {
      return undefined;
    }

    const timer = window.setInterval(() => {
      void loadJob(activeJobId, true);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeJobId]);

  useEffect(() => {
    setProjectStatusDraft(selectedProject?.status ?? "active");
    setProjectTagsDraft(selectedProject?.tags.join(", ") ?? "");
  }, [selectedProject]);

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
    setModelLoadState((current) => ({
      ...current,
      [targetMediaType]: "loading",
    }));
    try {
      const payload = await requestJson<ModelsResponse>(
        `/models?media_type=${encodeURIComponent(targetMediaType)}`,
      );
      const nextModels = payload.models.map(normalizeModelOption);
      setReadinessError(null);
      startTransition(() => {
        setModelOptionsByMedia((current) => ({
          ...current,
          [targetMediaType]: nextModels,
        }));
        setModelLoadState((current) => ({
          ...current,
          [targetMediaType]: "loaded",
        }));
        setDrafts((current) => {
          const currentDraft = current[targetMediaType] ?? defaultSubmitValues[targetMediaType];
          const currentModelId =
            typeof currentDraft.modelId === "string" ? currentDraft.modelId : "";
          if (
            currentModelId &&
            nextModels.some(
              (option) => option.id === currentModelId && option.isAvailable,
            )
          ) {
            return current;
          }

          const preferredModel =
            nextModels.find((option) => option.isAvailable && option.isDefault) ??
            nextModels.find((option) => option.isAvailable);
          if (!preferredModel && !currentModelId) {
            return current;
          }

          return {
            ...current,
            [targetMediaType]: {
              ...currentDraft,
              mediaType: targetMediaType,
              modelId: preferredModel?.id ?? "",
            },
          };
        });
      });
    } catch (error) {
      setModelLoadState((current) => ({
        ...current,
        [targetMediaType]: "error",
      }));
      setReadinessError(error instanceof Error ? error.message : "Failed to load models.");
    }
  }

  async function handleReadinessRetry(): Promise<void> {
    setReadinessError(null);
    const isReachable = await loadReadiness();
    if (isReachable) {
      await loadModels(mediaType);
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
      const galleryQuery = new URLSearchParams({
        media_type: targetMediaType,
        limit: "8",
      });
      if (selectedProjectId) {
        galleryQuery.set("project_id", selectedProjectId);
      }
      if (gallerySearch.trim()) {
        galleryQuery.set("q", gallerySearch.trim());
      }
      const [galleryPayload, metricsPayload, galleryStatsPayload] = await Promise.all([
        requestJson<GalleryItemResponse[]>(`/gallery?${galleryQuery.toString()}`),
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

      const resolvedGalleryPayload =
        detailPayload && !galleryPayload.some((item) => item.asset_id === detailPayload.asset_id)
          ? [detailPayload, ...galleryPayload].slice(0, 8)
          : galleryPayload;

      startTransition(() => {
        setGalleryItems(resolvedGalleryPayload);
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

  /**
   * Poll a job until it reaches a terminal status and return that status.
   *
   * The story and matrix panels drive their own multi-step flows, so they need to
   * wait on a job without taking over the composer's own submit state.
   */
  async function awaitJobCompletion(jobId: string): Promise<string> {
    const intervalMs = 1200;
    const deadline = Date.now() + 10 * 60 * 1000;
    for (;;) {
      const payload = await requestJson<JobResponse>(`/jobs/${jobId}`);
      if (terminalStatuses.has(payload.status)) {
        return payload.status;
      }
      if (Date.now() > deadline) {
        return payload.status;
      }
      await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
    }
  }

  async function handleCancelLatestJob(): Promise<void> {
    if (!latestJob || terminalStatuses.has(latestJob.status)) {
      return;
    }

    setErrorMessage(null);
    setStatusMessage("Cancelling job...");

    try {
      const payload = await requestJson<JobResponse>(`/jobs/${latestJob.id}/cancel`, {
        method: "POST",
      });
      startTransition(() => {
        setLatestJob(payload);
      });
      setActiveJobId(null);
      setIsSubmitting(false);
      setStatusMessage(`Job ${payload.id} cancelled.`);
      await refreshStudio(payload.media_type);
      await loadProjects();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to cancel job.");
    }
  }

  async function handleSubmit(values: PromptFormSubmitValues): Promise<void> {
    const requestedModelAvailable = modelOptionsByMedia[values.mediaType].some(
      (model) => model.id === values.modelId && model.isAvailable,
    );
    if (readinessState !== "ready" || !requestedModelAvailable) {
      setStatusMessage(
        readinessState !== "ready"
          ? readinessCopy
          : "選択中のモデルは利用できません。利用可能なモデルを選択してください。",
      );
      return;
    }

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

  async function handleCreateProject(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const trimmedName = newProjectName.trim();
    if (!trimmedName) {
      setErrorMessage("Project name is required.");
      return;
    }

    setIsProjectBusy(true);
    setErrorMessage(null);
    setAssetMessage(null);

    try {
      const project = await requestJson<ProjectResponse>("/projects", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: trimmedName,
          description: newProjectDescription.trim(),
        }),
      });
      startTransition(() => {
        setProjects((current) => [project, ...current.filter((item) => item.id !== project.id)]);
        setSelectedProjectId(project.id);
        setNewProjectName("");
        setNewProjectDescription("");
      });
      setAssetMessage(`Created project ${project.name}.`);
      await refreshStudio(mediaType);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to create project.");
    } finally {
      setIsProjectBusy(false);
    }
  }

  async function handleUpdateSelectedProject(): Promise<void> {
    if (!selectedProject) {
      return;
    }

    setIsProjectBusy(true);
    setErrorMessage(null);
    setAssetMessage(null);

    try {
      const project = await requestJson<ProjectResponse>(`/projects/${selectedProject.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          status: projectStatusDraft.trim() || "active",
          tags: projectTagsDraft
            .split(",")
            .map((tag) => tag.trim())
            .filter(Boolean),
        }),
      });
      startTransition(() => {
        setProjects((current) =>
          current.map((item) => (item.id === project.id ? project : item)),
        );
      });
      setAssetMessage(`Updated project ${project.name}.`);
      await refreshStudio(mediaType);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to update project.");
    } finally {
      setIsProjectBusy(false);
    }
  }

  async function handlePinSelectedAssetToProject(): Promise<void> {
    if (!selectedProject || !selectedAssetDetail) {
      return;
    }

    const pinnedAssetIds = Array.from(
      new Set([...selectedProject.pinned_asset_ids, selectedAssetDetail.asset_id]),
    );

    setIsProjectBusy(true);
    setErrorMessage(null);
    try {
      const project = await requestJson<ProjectResponse>(`/projects/${selectedProject.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ pinned_asset_ids: pinnedAssetIds }),
      });
      startTransition(() => {
        setProjects((current) =>
          current.map((item) => (item.id === project.id ? project : item)),
        );
      });
      setAssetMessage(`Pinned ${selectedAssetDetail.asset_id} to ${project.name}.`);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to pin asset.");
    } finally {
      setIsProjectBusy(false);
    }
  }

  async function handleFeedbackSubmit(values: FeedbackFormValues): Promise<boolean> {
    if (!selectedAssetDetail) {
      return false;
    }

    setIsFeedbackBusy(true);
    setErrorMessage(null);

    try {
      const feedback = await requestJson<FeedbackResponse>("/feedback", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          job_id: selectedAssetDetail.job_id,
          asset_id: selectedAssetDetail.asset_id,
          project_id: selectedAssetDetail.project_id,
          quality_rating: values.qualityRating,
          semantic_rating: values.semanticRating,
          creative_rating: values.creativeRating,
          reuse_intent: values.reuseIntent,
          export_ready: values.exportReady,
          issue_tags: values.issueTags,
          comments: values.comments,
          metadata: {
            source: "web-ui",
          },
        }),
      });
      setAssetMessage(`Saved feedback ${feedback.id}.`);
      await refreshStudio(selectedAssetDetail.media_type, {
        preferredAssetId: selectedAssetDetail.asset_id,
      });
      return true;
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to submit feedback.");
      return false;
    } finally {
      setIsFeedbackBusy(false);
    }
  }

  async function handleQuickReview(
    kind: "accept" | "revise" | "rerun",
    issueTags: QuickReviewIssueTag[] = [],
  ): Promise<boolean> {
    if (!selectedAssetDetail) {
      return false;
    }

    const reviewedAsset = selectedAssetDetail;
    const applicableIssueTags = issueTags.filter((tag) =>
      getQuickReviewIssueOptions(reviewedAsset.media_type).some((option) => option.id === tag),
    );
    setIsFeedbackBusy(true);
    setErrorMessage(null);
    try {
      await requestJson<FeedbackResponse>("/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: reviewedAsset.job_id,
          asset_id: reviewedAsset.asset_id,
          project_id: reviewedAsset.project_id,
          quality_rating: kind === "accept" ? 5 : 3,
          semantic_rating: kind === "accept" ? 5 : 3,
          creative_rating: kind === "accept" ? 5 : 3,
          reuse_intent: kind !== "accept",
          export_ready: kind === "accept",
          issue_tags: applicableIssueTags,
          comments: kind === "accept" ? "採用" : kind === "rerun" ? "作り直す" : "少し直す",
          metadata: { source: "quick-review", decision: kind },
        }),
      });
      if (kind === "accept") {
        setAssetMessage("採用として保存しました。書き出しまたはプロジェクトへの保存を続けられます。");
        await refreshStudio(reviewedAsset.media_type, {
          preferredAssetId: reviewedAsset.asset_id,
        });
        return true;
      }

      return await handleAssetReuse({
        action: kind === "rerun" ? "rerun" : "variation",
        issueTags: applicableIssueTags,
        sourceAsset: reviewedAsset,
        useSourceSnapshot: true,
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to save review.");
      return false;
    } finally {
      setIsFeedbackBusy(false);
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

  async function handleAssetReuse(options: AssetReuseOptions = {}): Promise<boolean> {
    const sourceAsset = options.sourceAsset ?? selectedAssetDetail;
    if (!sourceAsset) {
      return false;
    }

    const {
      action = "variation",
      issueTags = [],
      useSourceSnapshot = false,
    } = options;

    setIsAssetBusy(true);
    setErrorMessage(null);
    setAssetMessage(null);

    const snapshotValues = mergeDraftWithDefaults(
      sourceAsset.media_type,
      createDraftFromRequestSnapshot(sourceAsset.request_snapshot),
    );
    const sourceValues =
      useSourceSnapshot || mediaType !== sourceAsset.media_type
        ? snapshotValues
        : mergeDraftWithDefaults(sourceAsset.media_type, drafts[sourceAsset.media_type]);
    const reuseValues = {
      ...sourceValues,
      prompt: buildQuickReviewPrompt(sourceValues.prompt, issueTags),
      seed: action === "rerun" ? null : sourceValues.seed,
    };
    const projectId = useSourceSnapshot
      ? sourceAsset.project_id
      : selectedProjectId || sourceAsset.project_id || null;

    try {
      const payload = await requestJson<ReuseAssetResponse>(
        `/gallery/${sourceAsset.asset_id}/reuse`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(
            buildReusePayload(reuseValues, projectId, {
              action,
              params:
                issueTags.length > 0
                  ? {
                      review_issue_tags: issueTags,
                      review_source: "quick-review",
                    }
                  : undefined,
            }),
          ),
        },
      );

      setMediaType(sourceAsset.media_type);
      setSelectedProjectId(payload.project_id ?? "");
      setIsSubmitting(true);
      setActiveJobId(payload.job_id);
      setStatusMessage(
        action === "rerun" ? `Queued rerun job ${payload.job_id}.` : `Queued variation job ${payload.job_id}.`,
      );
      setAssetMessage(
        action === "rerun"
          ? `Created a fresh rerun from ${sourceAsset.asset_id}.`
          : `Created a reviewed variation from ${sourceAsset.asset_id}.`,
      );
      await loadJob(payload.job_id);
      await loadProjects();
      return true;
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to reuse the selected asset.");
      return false;
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
                aria-pressed={themeMode === "light"}
              >
                Light
              </button>
              <button
                type="button"
                className={`theme-switch__button ${themeMode === "dark" ? "is-active" : ""}`}
                onClick={() => setThemeMode("dark")}
                aria-pressed={themeMode === "dark"}
              >
                Dark
              </button>
            </div>
          </div>
        </section>

        <section className="section-card section-card--media">
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
                aria-pressed={mediaType === option}
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

        <section className="section-card section-card--snapshot section-card--projects">
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
              : "Create a project to start grouping jobs and assets."}
          </p>
          <details className="project-disclosure">
            <summary>New project</summary>
            <form className="project-create-form" onSubmit={(event) => void handleCreateProject(event)}>
              <label className="field-group field-group--full">
                <span>New project name</span>
                <input
                  type="text"
                  value={newProjectName}
                  onChange={(event) => setNewProjectName(event.target.value)}
                  placeholder="Campaign explorations"
                  disabled={isProjectBusy}
                />
              </label>
              <label className="field-group field-group--full">
                <span>Description</span>
                <textarea
                  rows={3}
                  value={newProjectDescription}
                  onChange={(event) => setNewProjectDescription(event.target.value)}
                  placeholder="Optional production note"
                  disabled={isProjectBusy}
                />
              </label>
              <button
                type="submit"
                className="secondary-button secondary-button--block"
                disabled={isProjectBusy || !newProjectName.trim()}
              >
                {isProjectBusy ? "Creating..." : "Create and select project"}
              </button>
            </form>
          </details>
          {selectedProject ? (
            <div className="form-section">
              <div className="form-section__header">
                <div>
                  <p className="eyebrow">Project Metadata</p>
                  <h3>{selectedProject.name}</h3>
                </div>
              </div>
              <label className="field-group field-group--full">
                <span>Status</span>
                <input
                  type="text"
                  value={projectStatusDraft}
                  onChange={(event) => setProjectStatusDraft(event.target.value)}
                  disabled={isProjectBusy}
                />
              </label>
              <label className="field-group field-group--full">
                <span>Tags</span>
                <input
                  type="text"
                  value={projectTagsDraft}
                  onChange={(event) => setProjectTagsDraft(event.target.value)}
                  placeholder="draft, review, delivery"
                  disabled={isProjectBusy}
                />
              </label>
              <div className="asset-actions">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    void handleUpdateSelectedProject();
                  }}
                  disabled={isProjectBusy}
                >
                  Update project
                </button>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    void handlePinSelectedAssetToProject();
                  }}
                  disabled={
                    isProjectBusy ||
                    !selectedAssetDetail ||
                    selectedAssetDetail.project_id !== selectedProject.id ||
                    selectedProject.pinned_asset_ids.includes(selectedAssetDetail.asset_id)
                  }
                >
                  Pin selected asset
                </button>
              </div>
              <p className="section-footnote">
                {selectedProject.pinned_asset_ids.length} pinned assets | status{" "}
                {selectedProject.status}
              </p>
            </div>
          ) : null}
        </section>

        <section className="section-card section-card--activity">
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
        <header className="studio-topbar">
          <div className="studio-topbar__identity">
            <p className="eyebrow">Workspace / {mediaTypeLabels[mediaType]}</p>
            <h1>{selectedProject?.name ?? "Unassigned workspace"}</h1>
            <p>
              Compose, review, and route local {mediaTypeLabels[mediaType].toLowerCase()} assets.
            </p>
          </div>
          <dl className="studio-topbar__stats" aria-label="Workspace summary">
            <div>
              <dt>Assets</dt>
              <dd>{galleryStats?.total_items ?? 0}</dd>
            </div>
            <div>
              <dt>Success</dt>
              <dd>{formatPercent(metrics?.success_rate)}</dd>
            </div>
            <div>
              <dt>Models</dt>
              <dd>{activeModels.filter((model) => model.isAvailable).length}</dd>
            </div>
          </dl>
        </header>

        <p
          className={`readiness-banner ${readinessState === "ready" ? "is-ready" : "is-warning"}`}
          role="status"
          aria-live="polite"
        >
          <span className="readiness-banner__copy">
            <strong>
              {readinessState === "ready"
                ? "準備完了"
                : readinessState === "offline"
                  ? "接続できません"
                  : readinessState === "models-unavailable"
                    ? "モデルが未配置です"
                    : readinessState === "models-error"
                      ? "モデルを確認できません"
                      : "準備状況を確認中"}
            </strong>
            <span>{readinessCopy}</span>
          </span>
          {readinessState !== "ready" ? (
            <button
              type="button"
              className="secondary-button readiness-banner__retry"
              onClick={() => {
                void handleReadinessRetry();
              }}
              disabled={activeModelLoadState === "loading"}
            >
              再試行
            </button>
          ) : null}
        </p>

        {errorMessage ? <p className="error-banner" role="alert">{errorMessage}</p> : null}
        {readinessError ? <p className="error-banner" role="alert">{readinessError}</p> : null}

        <section className="section-card section-card--stage">
          <div className="section-card__header">
            <div>
              <p className="eyebrow">かんたん作成</p>
              <h2>{mediaType === "image" ? "画像を作る" : `${mediaTypeLabels[mediaType]} generation`}</h2>
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
            submitLabel={isSubmitting ? "生成中..." : "生成する"}
            disabled={isSubmitting}
            canSubmit={!isSubmitting && generationGateMessage === null}
            statusMessage={generationGateMessage ?? statusMessage}
            onDraftChange={handleDraftChange}
            onSubmit={(values) => {
              void handleSubmit(values);
            }}
          />
        </section>

        <div className="workspace-grid story-matrix-workspace">
          <LatestJobPanel
            latestJob={latestJob}
            onCancel={() => {
              void handleCancelLatestJob();
            }}
          />

          <GalleryPanel
            mediaLabel={mediaTypeLabels[mediaType]}
            items={galleryItems}
            projectName={selectedProject?.name ?? null}
            search={gallerySearch}
            onSearchChange={setGallerySearch}
            selectedAssetId={selectedAssetId}
            onSelect={(assetId) => {
              void loadAssetDetail(assetId);
            }}
            disabled={isAssetBusy || isFeedbackBusy}
          />
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
            <AssetDetailPanel
              key={selectedAssetDetail.asset_id}
              detail={selectedAssetDetail}
              projects={projects}
              assetProjectId={selectedAssetProjectId}
              onAssetProjectIdChange={setSelectedAssetProjectId}
              isAssetBusy={isAssetBusy}
              isFeedbackBusy={isFeedbackBusy}
              onOpenQuickReview={() => setErrorMessage(null)}
              onQuickReview={handleQuickReview}
              onReuse={() => {
                void handleAssetReuse();
              }}
              onLoadIntoComposer={loadSelectedAssetIntoComposer}
              onExport={() => {
                void handleAssetExport();
              }}
              onBindProject={() => {
                void handleAssetProjectBinding();
              }}
              onSubmitFeedback={handleFeedbackSubmit}
            />
          ) : (
            <div className="empty-stage">
              <div>
                <h3>No asset selected</h3>
                <p>Select an item from the gallery to inspect its detail and actions.</p>
              </div>
            </div>
          )}
        </section>

        <div className="workspace-grid">
          <StoryPanel
            modelId=""
            projectId={selectedProjectId}
            awaitJob={awaitJobCompletion}
            onGenerateSceneImage={(scene) => {
              startTransition(() => {
                loadDraftIntoComposer("image", {
                  prompt: scene.image_prompt,
                  negativePrompt: scene.image_negative,
                  imageBriefSubject: scene.heading,
                });
              });
              setStatusMessage(
                `Scene "${scene.heading || scene.id}" をコンポーザに読み込みました。`,
              );
            }}
            onGenerateSceneNarration={(scene) => {
              startTransition(() => {
                loadDraftIntoComposer("audio", {
                  prompt: scene.narration,
                  mood: scene.bgm_mood,
                });
              });
              setStatusMessage(
                `Scene "${scene.heading || scene.id}" のナレーションを音声コンポーザに読み込みました。`,
              );
            }}
          />

          <MatrixPanel
            modelId=""
            projectId={selectedProjectId}
            onInspectItem={(item) => {
              if (item.job_id) {
                void loadJob(item.job_id);
              }
            }}
          />
        </div>

        <ModelsSummaryPanel
          models={activeModels}
          metrics={metrics}
          activeMetrics={activeMetrics}
          mediaLabel={mediaTypeLabels[mediaType]}
        />
      </main>
    </div>
  );
}

export default App;
