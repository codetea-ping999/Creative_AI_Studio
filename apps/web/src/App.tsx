import {
  startTransition,
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";
import {
  PromptForm,
  type LoraOption,
  type MediaType,
  type ModelOption,
  type PromptFormSubmitValues,
} from "./components/PromptForm";
import { AssemblyTimelinePanel } from "./components/AssemblyTimelinePanel";
import { AssetDetailPanel, type FeedbackFormValues } from "./components/AssetDetailPanel";
import { GalleryPanel } from "./components/GalleryPanel";
import { LatestJobPanel } from "./components/LatestJobPanel";
import { MatrixPanel } from "./components/MatrixPanel";
import { SetupPanel } from "./components/SetupPanel";
import { StoryPanel } from "./components/StoryPanel";
import {
  getMediaReadiness,
  mediaKindLabels,
  pickStartingMedia,
  startingMediaOrder,
  type MediaReadiness,
  type ModelLoadState,
} from "./lib/mediaReadiness";
import { getInstallGuide } from "./lib/modelInstallGuide";
import { buildGeneratePayload, buildReusePayload } from "./lib/payloads";
import {
  buildQuickReviewPrompt,
  getQuickReviewIssueOptions,
  type QuickReviewIssueTag,
} from "./lib/quickReview";
import {
  createDraftFromRequestSnapshot,
  defaultSubmitValues,
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
  type GalleryMediaType,
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
import { useJobPolling } from "./hooks/useJobPolling";
import {
  studioViewDescriptions,
  studioViewLabels,
  studioViews,
  useStudioView,
  viewHref,
} from "./hooks/useStudioView";
import { jobStatusLabel } from "./lib/jobStatus";

type ThemeMode = "light" | "dark";
const MEDIA_STORAGE_KEY = "creative-ai-studio.media-type";

function readStoredMediaType(): MediaType | null {
  try {
    const stored = window.localStorage.getItem(MEDIA_STORAGE_KEY);
    return stored === "image" || stored === "audio" || stored === "video" ? stored : null;
  } catch {
    return null;
  }
}

function storeMediaType(mediaType: MediaType): void {
  try {
    window.localStorage.setItem(MEDIA_STORAGE_KEY, mediaType);
  } catch {
    // Private windows can refuse storage; the choice just is not remembered.
  }
}

/**
 * Gallery assets can be `media_type: "text"` (Story surface output), but the
 * composer's reuse/quick-review/feedback flows below predate that and have no
 * text branch (no `defaultSubmitValues.text`, no text tab). Reusing a text
 * asset through the composer is out of scope here — this narrows back to the
 * type those flows already assumed.
 */
function toComposerMediaType(mediaType: GalleryMediaType): MediaType {
  return mediaType as MediaType;
}

type AssetReuseOptions = {
  action?: "rerun" | "variation" | "melody";
  issueTags?: QuickReviewIssueTag[];
  sourceAsset?: GalleryAssetDetailResponse;
  /**
   * Where the new request's generation fields come from:
   * - "source-request": nowhere; the reuse route fills every field from the
   *   asset's saved request, so the rerun is exactly that request.
   * - "source-snapshot": the asset's saved request, edited here (quick review).
   * - "composer": the composer draft when it is on the asset's media lane,
   *   otherwise the saved request.
   */
  basis?: "source-request" | "source-snapshot" | "composer";
};

function App() {
  const [view, setView] = useStudioView();
  const [themeMode, setThemeMode] = useState<ThemeMode>("dark");
  const [mediaType, setMediaTypeState] = useState<MediaType>(
    () => readStoredMediaType() ?? "image",
  );
  // Once someone picks a media type (or the studio picked a working one for
  // them), stop moving the composer on its own.
  const [mediaDecided, setMediaDecided] = useState(() => readStoredMediaType() !== null);
  const setMediaType = useCallback((next: MediaType) => {
    setMediaDecided(true);
    storeMediaType(next);
    setMediaTypeState(next);
  }, []);
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
  // The gallery asset each composer lane's draft was loaded from. Only such a
  // draft stands in for its asset on "Reuse and rerun"; any other draft may be
  // empty or belong to something else entirely.
  const [composerAssetIds, setComposerAssetIds] = useState<
    Partial<Record<MediaType, string>>
  >({});
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDescription, setNewProjectDescription] = useState("");
  const [projectStatusDraft, setProjectStatusDraft] = useState("active");
  const [projectTagsDraft, setProjectTagsDraft] = useState("");
  const [gallerySearch, setGallerySearch] = useState("");
  const [galleryBatchFilter, setGalleryBatchFilter] = useState<string | null>(null);
  const [galleryItems, setGalleryItems] = useState<GalleryItemResponse[]>([]);
  const [galleryStats, setGalleryStats] = useState<GalleryStatsResponse | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedAssetDetail, setSelectedAssetDetail] =
    useState<GalleryAssetDetailResponse | null>(null);
  const [selectedAssetProjectId, setSelectedAssetProjectId] = useState("");
  const [metrics, setMetrics] = useState<MetricsSummaryResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isAssetBusy, setIsAssetBusy] = useState(false);
  const [isProjectBusy, setIsProjectBusy] = useState(false);
  const [isFeedbackBusy, setIsFeedbackBusy] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [assetMessage, setAssetMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [apiReachable, setApiReachable] = useState<boolean | null>(null);
  const { latestJob, setLatestJob, setActiveJobId, loadJob } = useJobPolling({
    onJobUpdate: async (payload, refreshAfterFinish) => {
      if (terminalStatuses.has(payload.status)) {
        setIsSubmitting(false);
        setStatusMessage(
          payload.status === "succeeded"
            ? "生成が終わりました。"
            : payload.error_message || `ジョブは${jobStatusLabel(payload.status)}になりました。`,
        );
        if (refreshAfterFinish) {
          await refreshStudio(payload.media_type, { preferredJobId: payload.id });
          await loadProjects();
        }
      } else {
        setStatusMessage(`${jobStatusLabel(payload.status)}…`);
      }
    },
    onJobError: (error) => {
      setIsSubmitting(false);
      setErrorMessage(error instanceof Error ? error.message : "ジョブの状態を読み込めませんでした。");
    },
  });

  const activeModels = modelOptionsByMedia[mediaType];
  const readinessByMedia: Record<MediaType, MediaReadiness> = {
    image: getMediaReadiness(modelLoadState.image, modelOptionsByMedia.image),
    audio: getMediaReadiness(modelLoadState.audio, modelOptionsByMedia.audio),
    video: getMediaReadiness(modelLoadState.video, modelOptionsByMedia.video),
  };
  if (!mediaDecided) {
    // Adjusting state while rendering (not in an effect) avoids painting the
    // unusable lane first: open on a media type that can actually generate.
    const startingMedia = pickStartingMedia(mediaType, readinessByMedia);
    if (startingMedia !== null) {
      setMediaDecided(true);
      if (startingMedia !== mediaType) {
        setMediaTypeState(startingMedia);
      }
    }
  }
  const readyAlternatives = startingMediaOrder.filter(
    (media) => media !== mediaType && readinessByMedia[media] === "ready",
  );
  const activeModelLoadState = modelLoadState[mediaType];
  const activeDraft = drafts[mediaType] ?? defaultSubmitValues[mediaType];
  const selectedModelAvailable = activeModels.some(
    (model) => model.id === activeDraft.modelId && model.isAvailable,
  );
  const activeAvailableModelCount = activeModels.filter((model) => model.isAvailable).length;
  const selectedAudioModel = modelOptionsByMedia.audio.find(
    (model) => model.id === drafts.audio.modelId,
  );
  const canConditionMelody =
    mediaType === "audio" &&
    selectedAssetDetail?.media_type === "audio" &&
    /\.wav$/i.test(selectedAssetDetail.output_path) &&
    Boolean(
      selectedAudioModel?.isAvailable &&
        selectedAudioModel.tags.includes("melody-conditioning"),
    );
  const melodyConditioningMessage = canConditionMelody
    ? `この WAV を参照して ${selectedAudioModel?.displayName ?? "選んだモデル"} で作ります。`
    : mediaType !== "audio"
      ? "メロディ参照は「音声」に切り替えると使えます。"
      : !selectedAudioModel?.tags.includes("melody-conditioning")
        ? "音声の入力欄で、メロディ参照に対応したモデルを選んでください。"
        : !selectedAudioModel.isAvailable
          ? selectedAudioModel.availabilityMessage || "選んだメロディ参照モデルは使えません。"
          : "メロディ参照にはライブラリの WAV が必要です。";
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
      ? `${mediaKindLabels[mediaType]}モデル ${activeAvailableModelCount} 件を利用できます。初回生成は準備に時間がかかる場合があります。`
      : readinessState === "models-loading"
        ? `${mediaKindLabels[mediaType]}モデルを読み込んでいます。`
        : readinessState === "models-unavailable"
          ? `利用可能な${mediaKindLabels[mediaType]}モデルがありません。モデルを配置してから再試行してください。`
          : readinessState === "models-error"
            ? `${mediaKindLabels[mediaType]}モデルの一覧を読み込めません。再試行してください。`
            : readinessState === "offline"
              ? "API に接続できません。./scripts/start_studio.sh の起動状態を確認してください。"
              : "API とローカルモデルを確認しています。";
  const generationGateMessage =
    readinessState !== "ready"
      ? readinessCopy
      : !selectedModelAvailable
        ? "選択中のモデルは利用できません。利用可能なモデルを選択してください。"
        : null;
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
      setComposerAssetIds((current) => ({ ...current, [targetMedia]: undefined }));
      setMediaType(targetMedia);
      setComposerRevision((current) => current + 1);
    },
    [setMediaType],
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
    // Every lane, not just the composer's, so the media switch and the setup
    // screen can say what works on this machine.
    for (const media of startingMediaOrder) {
      void loadModels(media);
    }
  }, [apiReachable]);


  useEffect(() => {
    if (!apiReachable) {
      return;
    }
    // A batch belongs to one media type; carrying its filter across a media
    // type switch would just leave the gallery looking empty.
    setGalleryBatchFilter(null);
    void loadModels(mediaType);
    void refreshStudio(mediaType);
  }, [apiReachable, mediaType]);

  useEffect(() => {
    if (!apiReachable) {
      return;
    }
    void refreshStudio(mediaType);
  }, [apiReachable, selectedProjectId, gallerySearch, galleryBatchFilter]);

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
      setReadinessError(error instanceof Error ? error.message : "モデル一覧を読み込めませんでした。");
    }
  }

  async function handleReadinessRetry(): Promise<void> {
    setReadinessError(null);
    const isReachable = await loadReadiness();
    if (isReachable) {
      await Promise.all(startingMediaOrder.map((media) => loadModels(media)));
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
      setErrorMessage(error instanceof Error ? error.message : "成果物の詳細を読み込めませんでした。");
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
      if (galleryBatchFilter) {
        galleryQuery.set("batch_id", galleryBatchFilter);
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
      setErrorMessage(error instanceof Error ? error.message : "最新の状態を読み込めませんでした。");
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
    setStatusMessage("中断しています…");

    try {
      const payload = await requestJson<JobResponse>(`/jobs/${latestJob.id}/cancel`, {
        method: "POST",
      });
      startTransition(() => {
        setLatestJob(payload);
      });
      setActiveJobId(null);
      setIsSubmitting(false);
      setStatusMessage("ジョブを中断しました。");
      await refreshStudio(payload.media_type);
      await loadProjects();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "ジョブを中断できませんでした。");
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
    setStatusMessage("生成を受け付けています…");
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
      setStatusMessage("生成を受け付けました。順番に処理します。");
      await loadJob(created.job_id);
    } catch (error) {
      setIsSubmitting(false);
      setErrorMessage(error instanceof Error ? error.message : "生成を始められませんでした。");
      setStatusMessage(null);
    }
  }

  async function handleCreateProject(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const trimmedName = newProjectName.trim();
    if (!trimmedName) {
      setErrorMessage("プロジェクト名を入力してください。");
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
      setAssetMessage(`プロジェクト「${project.name}」を作成しました。`);
      await refreshStudio(mediaType);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "プロジェクトを作成できませんでした。");
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
      setAssetMessage(`プロジェクト「${project.name}」を更新しました。`);
      await refreshStudio(mediaType);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "プロジェクトを更新できませんでした。");
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
      setAssetMessage(`「${project.name}」に固定しました。`);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "成果物を固定できませんでした。");
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
      await requestJson<FeedbackResponse>("/feedback", {
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
      setAssetMessage("評価を保存しました。");
      await refreshStudio(toComposerMediaType(selectedAssetDetail.media_type), {
        preferredAssetId: selectedAssetDetail.asset_id,
      });
      return true;
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "評価を保存できませんでした。");
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
        await refreshStudio(toComposerMediaType(reviewedAsset.media_type), {
          preferredAssetId: reviewedAsset.asset_id,
        });
        return true;
      }

      return await handleAssetReuse({
        action: kind === "rerun" ? "rerun" : "variation",
        issueTags: applicableIssueTags,
        sourceAsset: reviewedAsset,
        basis: "source-snapshot",
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "判定を保存できませんでした。");
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
    const composerMediaType = toComposerMediaType(selectedAssetDetail.media_type);
    startTransition(() => {
      setDrafts((current) => ({
        ...current,
        [composerMediaType]: {
          ...defaultSubmitValues[composerMediaType],
          ...nextDraft,
        },
      }));
      setComposerAssetIds((current) => ({
        ...current,
        [composerMediaType]: selectedAssetDetail.asset_id,
      }));
      setSelectedProjectId(selectedAssetDetail.project_id ?? "");
      setMediaType(composerMediaType);
      setComposerRevision((current) => current + 1);
      setAssetMessage("入力欄に読み込みました。直してから生成すると別案として作ります。");
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
      basis = "composer",
    } = options;

    setIsAssetBusy(true);
    setErrorMessage(null);
    setAssetMessage(null);

    const sourceMediaType = toComposerMediaType(sourceAsset.media_type);
    const useSourceSnapshot = basis === "source-snapshot";
    const projectId = useSourceSnapshot
      ? sourceAsset.project_id
      : selectedProjectId || sourceAsset.project_id || null;
    let requestBody: Record<string, unknown>;
    if (basis === "source-request") {
      requestBody = { action, project_id: projectId };
    } else {
      const snapshotValues = mergeDraftWithDefaults(
        sourceMediaType,
        createDraftFromRequestSnapshot(sourceAsset.request_snapshot),
      );
      const sourceValues =
        useSourceSnapshot || mediaType !== sourceMediaType
          ? snapshotValues
          : mergeDraftWithDefaults(sourceMediaType, drafts[sourceMediaType]);
      const reuseValues = {
        ...sourceValues,
        prompt: buildQuickReviewPrompt(sourceValues.prompt, issueTags),
        seed: action === "rerun" ? null : sourceValues.seed,
      };
      requestBody = buildReusePayload(reuseValues, projectId, {
        action,
        params:
          issueTags.length > 0
            ? {
                review_issue_tags: issueTags,
                review_source: "quick-review",
              }
            : undefined,
      });
    }

    try {
      const payload = await requestJson<ReuseAssetResponse>(
        `/gallery/${sourceAsset.asset_id}/reuse`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(requestBody),
        },
      );

      setMediaType(sourceMediaType);
      setSelectedProjectId(payload.project_id ?? "");
      setIsSubmitting(true);
      setActiveJobId(payload.job_id);
      setStatusMessage(
        action === "rerun"
          ? "同じ条件での作り直しを受け付けました。"
          : action === "melody"
            ? "メロディ参照での生成を受け付けました。"
            : "別案の生成を受け付けました。",
      );
      setAssetMessage(
        action === "rerun"
          ? "選んだ成果物と同じ条件で作り直します。"
          : action === "melody"
            ? "選んだ成果物をメロディの参照に使います。"
          : "判定を反映した別案を作ります。",
      );
      await loadJob(payload.job_id);
      await loadProjects();
      return true;
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "選んだ成果物を再利用できませんでした。");
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
      setAssetMessage(`書き出しました: ${payload.export_path}`);
      await refreshStudio(toComposerMediaType(selectedAssetDetail.media_type), {
        preferredAssetId: selectedAssetDetail.asset_id,
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "選んだ成果物を書き出せませんでした。");
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
          ? "成果物をプロジェクトに入れました。"
          : "成果物をプロジェクトから外しました。",
      );
      await refreshStudio(toComposerMediaType(payload.media_type), { preferredAssetId: payload.asset_id });
      await loadProjects();
    } catch (error) {
      setErrorMessage(
        error instanceof Error ? error.message : "成果物のプロジェクトを変更できませんでした。",
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
            <h1>ローカル制作スタジオ</h1>
          </div>
          <nav className="view-nav" aria-label="画面">
            {studioViews.map((option) => (
              <a
                key={option}
                href={viewHref(option)}
                className={`view-nav__item ${view === option ? "is-active" : ""}`}
                aria-current={view === option ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  setView(option);
                }}
              >
                <strong>{studioViewLabels[option]}</strong>
                <span className="view-nav__meta">{studioViewDescriptions[option]}</span>
              </a>
            ))}
          </nav>
        </section>
      </aside>

      <main className="studio-main">
        <header className="studio-topbar">
          <div className="studio-topbar__identity">
            <h1>{studioViewLabels[view]}</h1>
            <p>{studioViewDescriptions[view]}</p>
          </div>
          <label className="field-group topbar-project">
            <span>保存先</span>
            <select
              value={selectedProjectId}
              onChange={(event) => setSelectedProjectId(event.target.value)}
            >
              <option value="">プロジェクトなし</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}（{project.asset_count} 件）
                </option>
              ))}
            </select>
          </label>
        </header>

        {view === "create" ? (
          <>
            <div className="view-toolbar">
              <span className="view-toolbar__label" id="media-switch-label">種類</span>
              <div className="mode-toggle media-switch" role="group" aria-labelledby="media-switch-label">
                {(["image", "audio", "video"] as MediaType[]).map((option) => (
                  <button
                    key={option}
                    type="button"
                    className={`mode-toggle__button ${mediaType === option ? "is-active" : ""}`}
                    onClick={() => setMediaType(option)}
                    aria-pressed={mediaType === option}
                  >
                    {mediaKindLabels[option]}
                    {readinessByMedia[option] === "missing" ? (
                      <span className="media-switch__note">準備が必要</span>
                    ) : null}
                  </button>
                ))}
              </div>
            </div>

            {errorMessage ? <p className="error-banner" role="alert">{errorMessage}</p> : null}
            {readinessError ? <p className="error-banner" role="alert">{readinessError}</p> : null}

            {readinessState === "models-unavailable" ? (
              <>
                <section className="section-card setup-callout" aria-labelledby="setup-callout-heading">
                  <p className="eyebrow">準備が必要</p>
                  <h2 id="setup-callout-heading">
                    {mediaKindLabels[mediaType]}のモデルがまだ置かれていません
                  </h2>
                  <p>
                    {mediaKindLabels[mediaType]}を作るには、次のどれかのモデルを models/
                    に置いてから「再確認」を押してください。
                  </p>
                  <ul className="setup-callout__models">
                    {activeModels.map((model) => (
                      <li key={model.id}>
                        <strong>{model.displayName}</strong>
                        <span>{getInstallGuide(mediaType, model)?.note ?? "配置先は準備画面で確認できます。"}</span>
                      </li>
                    ))}
                  </ul>
                  <div className="setup-callout__actions">
                    {readyAlternatives.map((media) => (
                      <button
                        key={media}
                        type="button"
                        className="primary-button"
                        onClick={() => setMediaType(media)}
                      >
                        {mediaKindLabels[media]}をつくる
                      </button>
                    ))}
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => setView("setup")}
                    >
                      準備画面で詳しく見る
                    </button>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => {
                        void handleReadinessRetry();
                      }}
                      disabled={activeModelLoadState === "loading"}
                    >
                      再確認
                    </button>
                  </div>
                </section>
              </>
            ) : (
              <>
                {readinessState !== "ready" ? (
                  <p
                    className="readiness-banner is-warning"
                    role="status"
                    aria-live="polite"
                  >
                    <span className="readiness-banner__copy">
                      <strong>
                        {readinessState === "offline"
                          ? "接続できません"
                          : readinessState === "models-error"
                            ? "モデルを確認できません"
                            : "準備状況を確認中"}
                      </strong>
                      <span>{readinessCopy}</span>
                    </span>
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
                  </p>
                ) : null}

                <section className="section-card section-card--stage">
                  <div className="section-card__header">
                    <div>
                      <p className="eyebrow">かんたん作成</p>
                      <h2>{mediaKindLabels[mediaType]}を作る</h2>
                    </div>
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
              </>
            )}

            <LatestJobPanel
              latestJob={latestJob}
              onCancel={handleCancelLatestJob}
              jobAssets={
                latestJob
                  ? galleryItems.filter((item) => item.job_id === latestJob.id)
                  : []
              }
              selectedAssetId={selectedAssetId}
              onSelectAsset={(assetId) => {
                void loadAssetDetail(assetId);
              }}
            />

            <section className="section-card" id="selected-asset">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">選んだ成果物</p>
                  <h2>判定・再利用・書き出し</h2>
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
                    // Edits made after "Load into composer" go out as a variation;
                    // otherwise the asset's own saved request reruns with a new seed.
                    const loadedIntoComposer =
                      composerAssetIds[toComposerMediaType(selectedAssetDetail.media_type)] ===
                      selectedAssetDetail.asset_id;
                    void handleAssetReuse(
                      loadedIntoComposer
                        ? { basis: "composer" }
                        : { action: "rerun", basis: "source-request" },
                    );
                  }}
                  canConditionMelody={canConditionMelody}
                  melodyConditioningMessage={melodyConditioningMessage}
                  onConditionMelody={() => {
                    void handleAssetReuse({ action: "melody" });
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
                    <h3>まだ選ばれていません</h3>
                    <p>生成が終わるとここに結果が出ます。過去の成果物はライブラリから選べます。</p>
                  </div>
                </div>
              )}
            </section>

            <p className="view-footer-link">
              <a
                href={viewHref("library")}
                onClick={(event) => {
                  event.preventDefault();
                  setView("library");
                }}
              >
                ライブラリで{mediaKindLabels[mediaType]}の一覧を見る
              </a>
            </p>
          </>
        ) : null}

        {view === "library" ? (
          <>
            <div className="view-toolbar">
              <span className="view-toolbar__label" id="library-media-label">種類</span>
              <div className="mode-toggle media-switch" role="group" aria-labelledby="library-media-label">
                {(["image", "audio", "video"] as MediaType[]).map((option) => (
                  <button
                    key={option}
                    type="button"
                    className={`mode-toggle__button ${mediaType === option ? "is-active" : ""}`}
                    onClick={() => setMediaType(option)}
                    aria-pressed={mediaType === option}
                  >
                    {mediaKindLabels[option]}
                  </button>
                ))}
              </div>
            </div>

            <GalleryPanel
              mediaLabel={mediaTypeLabels[mediaType]}
              items={galleryItems}
              projectName={selectedProject?.name ?? null}
              search={gallerySearch}
              onSearchChange={setGallerySearch}
              selectedAssetId={selectedAssetId}
              onSelect={(assetId) => {
                void loadAssetDetail(assetId).then(() => {
                  document.getElementById("selected-asset")?.scrollIntoView?.({ block: "start" });
                });
              }}
              disabled={isAssetBusy || isFeedbackBusy}
              activeBatchId={galleryBatchFilter}
              onFilterByBatch={setGalleryBatchFilter}
            />

            <section className="section-card" id="selected-asset">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">選んだ成果物</p>
                  <h2>判定・再利用・書き出し</h2>
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
                    // Edits made after "Load into composer" go out as a variation;
                    // otherwise the asset's own saved request reruns with a new seed.
                    const loadedIntoComposer =
                      composerAssetIds[toComposerMediaType(selectedAssetDetail.media_type)] ===
                      selectedAssetDetail.asset_id;
                    void handleAssetReuse(
                      loadedIntoComposer
                        ? { basis: "composer" }
                        : { action: "rerun", basis: "source-request" },
                    );
                  }}
                  canConditionMelody={canConditionMelody}
                  melodyConditioningMessage={melodyConditioningMessage}
                  onConditionMelody={() => {
                    void handleAssetReuse({ action: "melody" });
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
                    <h3>まだ選ばれていません</h3>
                    <p>生成が終わるとここに結果が出ます。過去の成果物はライブラリから選べます。</p>
                  </div>
                </div>
              )}
            </section>
          </>
        ) : null}

        {view !== "create" && errorMessage ? (
          <p className="error-banner" role="alert">{errorMessage}</p>
        ) : null}

        {view === "story" ? (
          <>
            <StoryPanel
              modelId=""
              projectId={selectedProjectId}
              awaitJob={awaitJobCompletion}
              // Empty ids let the API pick each media type's default model, so a
              // scene can be filled without first choosing models by hand.
              sceneModelIds={{ visual: "", narration: "", music: "" }}
              onGenerateSceneImage={(scene) => {
                setView("create");
                startTransition(() => {
                  loadDraftIntoComposer("image", {
                    prompt: scene.image_prompt,
                    negativePrompt: scene.image_negative,
                    imageBriefSubject: scene.heading,
                  });
                });
                setStatusMessage(
                  `シーン「${scene.heading || scene.id}」を入力欄に読み込みました。`,
                );
              }}
              onGenerateSceneNarration={(scene) => {
                setView("create");
                startTransition(() => {
                  loadDraftIntoComposer("audio", {
                    prompt: scene.narration,
                    mood: scene.bgm_mood,
                  });
                });
                setStatusMessage(
                  `シーン「${scene.heading || scene.id}」のナレーションを音声の入力欄に読み込みました。`,
                );
              }}
            />

            <AssemblyTimelinePanel projectId={selectedProjectId} />
          </>
        ) : null}

        {view === "compare" ? (
          <MatrixPanel
            modelId=""
            projectId={selectedProjectId}
            onInspectItem={(item) => {
              if (item.job_id) {
                setView("create");
                void loadJob(item.job_id);
              }
            }}
          />
        ) : null}

        {view === "setup" ? (
          <>
            <SetupPanel
              modelsByMedia={modelOptionsByMedia}
              readinessByMedia={readinessByMedia}
              metrics={metrics}
              galleryStats={galleryStats}
              onRetry={() => {
                void handleReadinessRetry();
              }}
              isRetrying={Object.values(modelLoadState).includes("loading")}
            />

            <section className="section-card section-card--projects">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">保存先</p>
                  <h2>プロジェクト</h2>
                </div>
              </div>
              <p className="section-footnote">
                {projects.length > 0
                  ? `${projects.length} 件のプロジェクトがあります。生成の保存先は画面上部で選べます。`
                  : "プロジェクトを作ると、生成した成果物をまとめて管理できます。"}
              </p>
              <details className="project-disclosure">
                <summary>新しいプロジェクト</summary>
                <form className="project-create-form" onSubmit={(event) => void handleCreateProject(event)}>
                  <label className="field-group field-group--full">
                    <span>プロジェクト名</span>
                    <input
                      type="text"
                      value={newProjectName}
                      onChange={(event) => setNewProjectName(event.target.value)}
                      placeholder="例: 春のキャンペーン"
                      disabled={isProjectBusy}
                    />
                  </label>
                  <label className="field-group field-group--full">
                    <span>メモ</span>
                    <textarea
                      rows={3}
                      value={newProjectDescription}
                      onChange={(event) => setNewProjectDescription(event.target.value)}
                      placeholder="任意"
                      disabled={isProjectBusy}
                    />
                  </label>
                  <button
                    type="submit"
                    className="secondary-button secondary-button--block"
                    disabled={isProjectBusy || !newProjectName.trim()}
                  >
                    {isProjectBusy ? "作成中..." : "作成して保存先にする"}
                  </button>
                </form>
              </details>
              {selectedProject ? (
                <div className="form-section">
                  <div className="form-section__header">
                    <div>
                      <p className="eyebrow">選択中のプロジェクト</p>
                      <h3>{selectedProject.name}</h3>
                    </div>
                  </div>
                  <label className="field-group field-group--full">
                    <span>状態</span>
                    <input
                      type="text"
                      value={projectStatusDraft}
                      onChange={(event) => setProjectStatusDraft(event.target.value)}
                      disabled={isProjectBusy}
                    />
                  </label>
                  <label className="field-group field-group--full">
                    <span>タグ</span>
                    <input
                      type="text"
                      value={projectTagsDraft}
                      onChange={(event) => setProjectTagsDraft(event.target.value)}
                      placeholder="例: 下書き, 確認中, 納品"
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
                      更新する
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
                      選択中の成果物を固定
                    </button>
                  </div>
                  <p className="section-footnote">
                    固定した成果物 {selectedProject.pinned_asset_ids.length} 件・状態{" "}
                    {selectedProject.status}
                  </p>
                </div>
              ) : null}
            </section>

            <section className="section-card" aria-labelledby="setup-display-heading">
              <div className="section-card__header">
                <div>
                  <p className="eyebrow">表示</p>
                  <h2 id="setup-display-heading">画面の明るさ</h2>
                </div>
                <div className="mode-toggle" role="group" aria-label="画面の明るさ">
                  <button
                    type="button"
                    className={`mode-toggle__button ${themeMode === "light" ? "is-active" : ""}`}
                    onClick={() => setThemeMode("light")}
                    aria-pressed={themeMode === "light"}
                  >
                    ライト
                  </button>
                  <button
                    type="button"
                    className={`mode-toggle__button ${themeMode === "dark" ? "is-active" : ""}`}
                    onClick={() => setThemeMode("dark")}
                    aria-pressed={themeMode === "dark"}
                  >
                    ダーク
                  </button>
                </div>
              </div>
            </section>
          </>
        ) : null}
      </main>
    </div>
  );
}

export default App;
