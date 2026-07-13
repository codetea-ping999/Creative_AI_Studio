import { startTransition, useCallback, useEffect, useState, type FormEvent } from "react";
import {
  PromptForm,
  type LoraOption,
  type MediaType,
  type ModelOption,
  type PromptFormSubmitValues,
} from "./components/PromptForm";
import { OutputThumbnail, StagePreview } from "./components/MediaPreview";
import { buildGeneratePayload, buildReusePayload } from "./lib/payloads";
import {
  buildQuickReviewPrompt,
  getQuickReviewIssueOptions,
  type QuickReviewIssueTag,
} from "./lib/quickReview";
import {
  createDraftFromRequestSnapshot,
  defaultSubmitValues,
  extractJobQualityScore,
  formatDate,
  formatPercent,
  formatScore,
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
  const [feedbackQuality, setFeedbackQuality] = useState("4");
  const [feedbackSemantic, setFeedbackSemantic] = useState("4");
  const [feedbackCreative, setFeedbackCreative] = useState("4");
  const [feedbackReuseIntent, setFeedbackReuseIntent] = useState(false);
  const [feedbackExportReady, setFeedbackExportReady] = useState(false);
  const [feedbackIssueTags, setFeedbackIssueTags] = useState("");
  const [feedbackComments, setFeedbackComments] = useState("");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [assetMessage, setAssetMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [apiReachable, setApiReachable] = useState<boolean | null>(null);
  const [isQuickReviewOpen, setIsQuickReviewOpen] = useState(false);
  const [quickReviewIssueTags, setQuickReviewIssueTags] = useState<QuickReviewIssueTag[]>([]);

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

  useEffect(() => {
    setIsQuickReviewOpen(false);
    setQuickReviewIssueTags([]);
  }, [selectedAssetId]);

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

  async function handleFeedbackSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!selectedAssetDetail) {
      return;
    }

    const qualityRating = Number.parseInt(feedbackQuality, 10);
    const semanticRating = Number.parseInt(feedbackSemantic, 10);
    const creativeRating = Number.parseInt(feedbackCreative, 10);

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
          quality_rating: qualityRating,
          semantic_rating: Number.isFinite(semanticRating) ? semanticRating : null,
          creative_rating: Number.isFinite(creativeRating) ? creativeRating : null,
          reuse_intent: feedbackReuseIntent,
          export_ready: feedbackExportReady,
          issue_tags: feedbackIssueTags
            .split(",")
            .map((tag) => tag.trim())
            .filter(Boolean),
          comments: feedbackComments.trim(),
          metadata: {
            source: "web-ui",
          },
        }),
      });
      setFeedbackComments("");
      setFeedbackIssueTags("");
      setAssetMessage(`Saved feedback ${feedback.id}.`);
      await refreshStudio(selectedAssetDetail.media_type, {
        preferredAssetId: selectedAssetDetail.asset_id,
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to submit feedback.");
    } finally {
      setIsFeedbackBusy(false);
    }
  }

  async function handleQuickReview(
    kind: "accept" | "revise" | "rerun",
    issueTags: QuickReviewIssueTag[] = [],
  ): Promise<void> {
    if (!selectedAssetDetail) {
      return;
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
        setIsQuickReviewOpen(false);
        return;
      }

      const queued = await handleAssetReuse({
        action: kind === "rerun" ? "rerun" : "variation",
        issueTags: applicableIssueTags,
        sourceAsset: reviewedAsset,
        useSourceSnapshot: true,
      });
      if (queued) {
        setQuickReviewIssueTags([]);
        setIsQuickReviewOpen(false);
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Failed to save review.");
    } finally {
      setIsFeedbackBusy(false);
    }
  }

  function openQuickReview(): void {
    setErrorMessage(null);
    setQuickReviewIssueTags([]);
    setIsQuickReviewOpen(true);
  }

  function toggleQuickReviewIssueTag(issueTag: QuickReviewIssueTag): void {
    setQuickReviewIssueTags((current) =>
      current.includes(issueTag)
        ? current.filter((tag) => tag !== issueTag)
        : [...current, issueTag],
    );
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
                  <span className={`status-chip status-chip--${latestJob.status}`} role="status">
                    {latestJob.status}
                  </span>
                  <span className="history-score">{formatPercent(latestJob.progress * 100)}</span>
                </div>
                {!terminalStatuses.has(latestJob.status) ? (
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => {
                      void handleCancelLatestJob();
                    }}
                  >
                    Cancel job
                  </button>
                ) : null}
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
              <p className="section-footnote">
                {galleryItems.length} items loaded
                {selectedProject ? ` | ${selectedProject.name}` : ""}
              </p>
            </div>
            <label className="field-group field-group--full">
              <span>Search current gallery</span>
              <input
                type="search"
                value={gallerySearch}
                onChange={(event) => setGallerySearch(event.target.value)}
                placeholder="prompt, model, project, metadata"
              />
            </label>
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
                    disabled={isAssetBusy || isFeedbackBusy}
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
                      void handleQuickReview("accept");
                    }}
                    disabled={isFeedbackBusy || isAssetBusy}
                  >
                    採用
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={openQuickReview}
                    disabled={isFeedbackBusy || isAssetBusy}
                    aria-expanded={isQuickReviewOpen}
                  >
                    少し直す
                  </button>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => {
                      void handleQuickReview("rerun");
                    }}
                    disabled={isFeedbackBusy || isAssetBusy}
                  >
                    作り直す
                  </button>
                </div>
                {isQuickReviewOpen ? (
                  <fieldset className="quick-review-panel">
                    <legend>どこを直しますか？</legend>
                    <p>選んだ修正理由を保存し、その内容を反映した派生案を作ります。</p>
                    <div className="quick-review-options">
                      {getQuickReviewIssueOptions(selectedAssetDetail.media_type).map((option) => {
                        const isSelected = quickReviewIssueTags.includes(option.id);
                        return (
                          <label
                            key={option.id}
                            className={`quick-review-option ${isSelected ? "is-selected" : ""}`}
                          >
                            <input
                              type="checkbox"
                              checked={isSelected}
                              onChange={() => toggleQuickReviewIssueTag(option.id)}
                              disabled={isFeedbackBusy || isAssetBusy}
                            />
                            <span>{option.label}</span>
                          </label>
                        );
                      })}
                    </div>
                    <div className="asset-actions">
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => {
                          setIsQuickReviewOpen(false);
                          setQuickReviewIssueTags([]);
                        }}
                        disabled={isFeedbackBusy || isAssetBusy}
                      >
                        キャンセル
                      </button>
                      <button
                        type="button"
                        className="dock-submit"
                        onClick={() => {
                          void handleQuickReview("revise", quickReviewIssueTags);
                        }}
                        disabled={
                          isFeedbackBusy || isAssetBusy || quickReviewIssueTags.length === 0
                        }
                      >
                        選んだ内容で再生成
                      </button>
                    </div>
                  </fieldset>
                ) : null}
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

                <form className="form-section" onSubmit={(event) => void handleFeedbackSubmit(event)}>
                  <div className="form-section__header">
                    <div>
                      <p className="eyebrow">Human Feedback</p>
                      <h3>Record review signal for calibration and reuse decisions</h3>
                    </div>
                  </div>
                  <div className="field-grid field-grid--controls">
                    <label className="field-group">
                      <span>Quality</span>
                      <select
                        value={feedbackQuality}
                        onChange={(event) => setFeedbackQuality(event.target.value)}
                        disabled={isFeedbackBusy}
                      >
                        {[1, 2, 3, 4, 5].map((rating) => (
                          <option key={rating} value={rating}>
                            {rating}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field-group">
                      <span>Semantic</span>
                      <select
                        value={feedbackSemantic}
                        onChange={(event) => setFeedbackSemantic(event.target.value)}
                        disabled={isFeedbackBusy}
                      >
                        {[1, 2, 3, 4, 5].map((rating) => (
                          <option key={rating} value={rating}>
                            {rating}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field-group">
                      <span>Creative</span>
                      <select
                        value={feedbackCreative}
                        onChange={(event) => setFeedbackCreative(event.target.value)}
                        disabled={isFeedbackBusy}
                      >
                        {[1, 2, 3, 4, 5].map((rating) => (
                          <option key={rating} value={rating}>
                            {rating}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div className="feedback-toggles">
                    <label>
                      <input
                        type="checkbox"
                        checked={feedbackReuseIntent}
                        onChange={(event) => setFeedbackReuseIntent(event.target.checked)}
                        disabled={isFeedbackBusy}
                      />
                      Reuse candidate
                    </label>
                    <label>
                      <input
                        type="checkbox"
                        checked={feedbackExportReady}
                        onChange={(event) => setFeedbackExportReady(event.target.checked)}
                        disabled={isFeedbackBusy}
                      />
                      Export ready
                    </label>
                  </div>
                  <label className="field-group field-group--full">
                    <span>Issue tags</span>
                    <input
                      type="text"
                      value={feedbackIssueTags}
                      onChange={(event) => setFeedbackIssueTags(event.target.value)}
                      placeholder="composition, lighting"
                      disabled={isFeedbackBusy}
                    />
                  </label>
                  <label className="field-group field-group--full">
                    <span>Comments</span>
                    <textarea
                      rows={3}
                      value={feedbackComments}
                      onChange={(event) => setFeedbackComments(event.target.value)}
                      placeholder="What should change before production use?"
                      disabled={isFeedbackBusy}
                    />
                  </label>
                  <button type="submit" className="dock-submit" disabled={isFeedbackBusy}>
                    {isFeedbackBusy ? "Saving..." : "Save feedback"}
                  </button>
                </form>

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

export default App;
