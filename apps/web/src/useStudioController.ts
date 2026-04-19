import {
  startTransition,
  useDeferredValue,
  useEffect,
  useEffectEvent,
  useState,
} from "react";
import type {
  ControlMode,
  MediaType,
  ModelOption,
  PromptFormSubmitValues,
} from "./components/promptFormTypes";
import { studioClient, type ProjectListFilters } from "./studioClient";
import {
  buildFeedbackPayload,
  buildGeneratePayload,
  buildProjectPayload,
  buildReusePayload,
  createEmptyFeedbackFormValues,
  createDraftFromRequestSnapshot,
  createEmptyProjectFormValues,
  defaultSubmitValues,
  formatJobStatus,
  mergeDraftWithDefaults,
  normalizeLoraOption,
  normalizeModelOption,
  terminalStatuses,
  type GalleryAssetDetailResponse,
  type GalleryItemResponse,
  type JobResponse,
  type FeedbackFormValues,
  type MetricsSummaryResponse,
  type ProjectFormValues,
  type ProjectJobsResponse,
  type ProjectResponse,
  type RefreshStudioOptions,
  type ThemeMode,
} from "./studio";

type UrlState = {
  mediaType: MediaType;
  selectedProjectId: string;
  projectSearchText: string;
  projectStatusFilter: string;
  projectTagFilter: string;
  controlModes: Record<MediaType, ControlMode>;
};

const mediaTypes: MediaType[] = ["image", "audio", "video"];

const defaultControlModes: Record<MediaType, ControlMode> = {
  image: "quick",
  audio: "quick",
  video: "quick",
};

const emptyModelOptionsByMedia: Record<MediaType, ModelOption[]> = {
  image: [],
  audio: [],
  video: [],
};

function parseMediaType(value: string | null): MediaType | null {
  return value === "image" || value === "audio" || value === "video" ? value : null;
}

function parseControlMode(value: string | null): ControlMode | null {
  return value === "quick" || value === "advanced" ? value : null;
}

function readUrlState(): UrlState {
  if (typeof window === "undefined") {
    return {
      mediaType: "image",
      selectedProjectId: "",
      projectSearchText: "",
      projectStatusFilter: "",
      projectTagFilter: "",
      controlModes: { ...defaultControlModes },
    };
  }

  const params = new URLSearchParams(window.location.search);

  return {
    mediaType: parseMediaType(params.get("mediaType")) ?? "image",
    selectedProjectId: params.get("projectId") ?? "",
    projectSearchText: params.get("q") ?? "",
    projectStatusFilter: params.get("status") ?? "",
    projectTagFilter: params.get("tag") ?? "",
    controlModes: {
      image: parseControlMode(params.get("imageMode")) ?? "quick",
      audio: parseControlMode(params.get("audioMode")) ?? "quick",
      video: parseControlMode(params.get("videoMode")) ?? "quick",
    },
  };
}

function getErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function useStudioController() {
  const [initialUrlState] = useState<UrlState>(() => readUrlState());
  const [themeMode, setThemeMode] = useState<ThemeMode>("light");
  const [mediaType, setMediaType] = useState<MediaType>(initialUrlState.mediaType);
  const [composerRevision, setComposerRevision] = useState(0);
  const [modelOptionsByMedia, setModelOptionsByMedia] =
    useState<Record<MediaType, ModelOption[]>>(emptyModelOptionsByMedia);
  const [loraOptions, setLoraOptions] = useState<ReturnType<typeof normalizeLoraOption>[]>([]);
  const [drafts, setDrafts] = useState<Record<MediaType, Partial<PromptFormSubmitValues>>>({
    image: defaultSubmitValues.image,
    audio: defaultSubmitValues.audio,
    video: defaultSubmitValues.video,
  });
  const [controlModes, setControlModes] = useState<Record<MediaType, ControlMode>>({
    ...initialUrlState.controlModes,
  });
  const [projectCatalog, setProjectCatalog] = useState<ProjectResponse[]>([]);
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [projectSearchText, setProjectSearchText] = useState(initialUrlState.projectSearchText);
  const [projectStatusFilter, setProjectStatusFilter] = useState(
    initialUrlState.projectStatusFilter,
  );
  const [projectTagFilter, setProjectTagFilter] = useState(initialUrlState.projectTagFilter);
  const deferredProjectSearchText = useDeferredValue(projectSearchText.trim());
  const [createProjectForm, setCreateProjectForm] = useState<ProjectFormValues>(
    createEmptyProjectFormValues(),
  );
  const [selectedProjectId, setSelectedProjectId] = useState(initialUrlState.selectedProjectId);
  const [selectedProjectData, setSelectedProjectData] = useState<ProjectJobsResponse | null>(null);
  const [galleryItems, setGalleryItems] = useState<GalleryItemResponse[]>([]);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedAssetDetail, setSelectedAssetDetail] =
    useState<GalleryAssetDetailResponse | null>(null);
  const [selectedAssetProjectId, setSelectedAssetProjectId] = useState("");
  const [feedbackForm, setFeedbackForm] = useState<FeedbackFormValues>(
    createEmptyFeedbackFormValues(),
  );
  const [latestJob, setLatestJob] = useState<JobResponse | null>(null);
  const [metricsSummary, setMetricsSummary] = useState<MetricsSummaryResponse | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isAssetBusy, setIsAssetBusy] = useState(false);
  const [isFeedbackBusy, setIsFeedbackBusy] = useState(false);
  const [isProjectBusy, setIsProjectBusy] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [assetMessage, setAssetMessage] = useState<string | null>(null);
  const [projectMessage, setProjectMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isProjectDirty, setIsProjectDirty] = useState(false);

  const activeModels = modelOptionsByMedia[mediaType];
  const activeControlMode = controlModes[mediaType];
  const selectedProjectName = selectedProjectData?.project.name ?? null;
  const prioritizedGalleryItems = selectedProjectId
    ? [...galleryItems].sort((left, right) => {
        const leftMatches = left.project_id === selectedProjectId ? 1 : 0;
        const rightMatches = right.project_id === selectedProjectId ? 1 : 0;
        if (leftMatches !== rightMatches) {
          return rightMatches - leftMatches;
        }
        return right.updated_at.localeCompare(left.updated_at);
      })
    : galleryItems;
  const selectedProjectGalleryCount = selectedProjectId
    ? prioritizedGalleryItems.filter((item) => item.project_id === selectedProjectId).length
    : 0;
  const reviewJob = selectedProjectData
    ? selectedProjectData.jobs
        .slice()
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0] ?? null
    : latestJob;
  const availableStatuses = Array.from(
    new Set(
      projectCatalog
        .map((project) => project.status.trim())
        .filter((status) => status.length > 0),
    ),
  ).sort((left, right) => left.localeCompare(right));
  const availableTags = Array.from(
    new Set(projectCatalog.flatMap((project) => project.tags)),
  ).sort((left, right) => left.localeCompare(right));

  useEffect(() => {
    document.documentElement.setAttribute("data-bs-theme", themeMode);
    document.documentElement.style.colorScheme = themeMode;
  }, [themeMode]);

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!isProjectDirty) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    };

    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [isProjectDirty]);

  useEffect(() => {
    const handlePopState = () => {
      const nextState = readUrlState();
      startTransition(() => {
        setMediaType(nextState.mediaType);
        setSelectedProjectId(nextState.selectedProjectId);
        setProjectSearchText(nextState.projectSearchText);
        setProjectStatusFilter(nextState.projectStatusFilter);
        setProjectTagFilter(nextState.projectTagFilter);
        setControlModes(nextState.controlModes);
      });
    };

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    let changed = false;

    if (selectedProjectId) {
      if (params.get("projectId") !== selectedProjectId) {
        params.set("projectId", selectedProjectId);
        changed = true;
      }
    } else if (params.has("projectId")) {
      params.delete("projectId");
      changed = true;
    }

    if (params.get("mediaType") !== mediaType) {
      params.set("mediaType", mediaType);
      changed = true;
    }

    for (const mode of mediaTypes) {
      const key = `${mode}Mode`;
      if (params.get(key) !== controlModes[mode]) {
        params.set(key, controlModes[mode]);
        changed = true;
      }
    }

    if (projectSearchText) {
      if (params.get("q") !== projectSearchText) {
        params.set("q", projectSearchText);
        changed = true;
      }
    } else if (params.has("q")) {
      params.delete("q");
      changed = true;
    }

    if (projectStatusFilter) {
      if (params.get("status") !== projectStatusFilter) {
        params.set("status", projectStatusFilter);
        changed = true;
      }
    } else if (params.has("status")) {
      params.delete("status");
      changed = true;
    }

    if (projectTagFilter) {
      if (params.get("tag") !== projectTagFilter) {
        params.set("tag", projectTagFilter);
        changed = true;
      }
    } else if (params.has("tag")) {
      params.delete("tag");
      changed = true;
    }

    if (changed) {
      window.history.replaceState(null, "", `?${params.toString()}`);
    }
  }, [
    controlModes,
    mediaType,
    projectSearchText,
    projectStatusFilter,
    projectTagFilter,
    selectedProjectId,
  ]);

  async function loadProjectCatalog(): Promise<void> {
    try {
      const payload = await studioClient.listProjects();
      startTransition(() => {
        setProjectCatalog(payload);
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "プロジェクト一覧の読み込みに失敗しました。"));
    }
  }

  async function loadProjects(filters: ProjectListFilters): Promise<void> {
    try {
      const payload = await studioClient.listProjects(filters);
      startTransition(() => {
        setProjects(payload);
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "プロジェクトの取得に失敗しました。"));
    }
  }

  async function loadProjectWorkspace(projectId: string): Promise<void> {
    try {
      const payload = await studioClient.getProjectWorkspace(projectId);
      startTransition(() => {
        setSelectedProjectData(payload);
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "選択中プロジェクトの読み込みに失敗しました。"));
    }
  }

  async function loadMetricsSummary(): Promise<void> {
    try {
      const payload = await studioClient.getMetricsSummary();
      startTransition(() => {
        setMetricsSummary(payload);
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "メトリクスの読み込みに失敗しました。"));
    }
  }

  async function refreshProjectViews(projectId: string | null): Promise<void> {
    await Promise.all([
      loadProjectCatalog(),
      loadProjects({
        query: deferredProjectSearchText,
        status: projectStatusFilter,
        tag: projectTagFilter,
      }),
      projectId ? loadProjectWorkspace(projectId) : Promise.resolve(),
    ]);
  }

  async function loadLoras(): Promise<void> {
    try {
      const payload = await studioClient.listLoras();
      startTransition(() => {
        setLoraOptions(payload.items.map(normalizeLoraOption));
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "LoRA カタログの読み込みに失敗しました。"));
    }
  }

  async function loadModels(targetMediaType: MediaType): Promise<void> {
    try {
      const payload = await studioClient.listModels(targetMediaType);
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
      setErrorMessage(getErrorMessage(error, "モデル一覧の読み込みに失敗しました。"));
    }
  }

  async function loadAssetDetail(assetId: string): Promise<void> {
    try {
      const payload = await studioClient.getGalleryAssetDetail(assetId);
      startTransition(() => {
        setSelectedAssetId(payload.asset_id);
        setSelectedAssetDetail(payload);
        setSelectedAssetProjectId(payload.project_id ?? "");
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "素材の詳細読み込みに失敗しました。"));
    }
  }

  async function refreshStudio(
    targetMediaType: MediaType,
    options: RefreshStudioOptions = {},
  ): Promise<void> {
    try {
      const galleryPayload = await studioClient.listGallery(targetMediaType, 8);

      let detailPayload: GalleryAssetDetailResponse | null = null;
      let nextAssetId: string | null = null;

      if (options.preferredJobId) {
        try {
          detailPayload = await studioClient.getGalleryAssetDetailByJob(options.preferredJobId);
          nextAssetId = detailPayload.asset_id;
        } catch (error) {
          console.error(error);
        }
      }

      if (detailPayload === null) {
        const candidateAssetId =
          options.preferredAssetId &&
          galleryPayload.some((item) => item.asset_id === options.preferredAssetId)
            ? options.preferredAssetId
            : galleryPayload.some((item) => item.asset_id === selectedAssetId)
              ? selectedAssetId
              : galleryPayload[0]?.asset_id ?? null;

        if (candidateAssetId) {
          detailPayload = await studioClient.getGalleryAssetDetail(candidateAssetId);
          nextAssetId = candidateAssetId;
        }
      }

      startTransition(() => {
        setGalleryItems(galleryPayload);
        setSelectedAssetId(nextAssetId);
        setSelectedAssetDetail(detailPayload);
        setSelectedAssetProjectId(detailPayload?.project_id ?? "");
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "Studio 画面の更新に失敗しました。"));
    }
  }

  async function loadJob(jobId: string, refreshAfterFinish = false): Promise<void> {
    try {
      const payload = await studioClient.getJob(jobId);
      startTransition(() => {
        setLatestJob(payload);
      });

      if (terminalStatuses.has(payload.status)) {
        setActiveJobId(null);
        setIsSubmitting(false);
        setStatusMessage(
          payload.status === "succeeded"
            ? "生成が完了しました。"
            : payload.error_message || `現在の状態: ${formatJobStatus(payload.status)}`,
        );

        if (refreshAfterFinish) {
          await Promise.all([
            refreshStudio(payload.media_type, { preferredJobId: payload.id }),
            refreshProjectViews(
              payload.project_id && payload.project_id === selectedProjectId
                ? payload.project_id
                : selectedProjectId || null,
            ),
            loadMetricsSummary(),
          ]);
        }
      } else {
        setStatusMessage(`現在の状態: ${formatJobStatus(payload.status)}`);
      }
    } catch (error) {
      setActiveJobId(null);
      setIsSubmitting(false);
      setErrorMessage(getErrorMessage(error, "生成ジョブの状態取得に失敗しました。"));
    }
  }

  const pollActiveJob = useEffectEvent((jobId: string) => {
    void loadJob(jobId, true);
  });

  useEffect(() => {
    void loadLoras();
    void loadProjectCatalog();
    void loadMetricsSummary();
  }, []);

  useEffect(() => {
    startTransition(() => {
      setFeedbackForm(createEmptyFeedbackFormValues());
    });
  }, [selectedAssetDetail?.asset_id]);

  useEffect(() => {
    void loadModels(mediaType);
    void refreshStudio(mediaType);
  }, [mediaType]);

  useEffect(() => {
    void loadProjects({
      query: deferredProjectSearchText,
      status: projectStatusFilter,
      tag: projectTagFilter,
    });
  }, [deferredProjectSearchText, projectStatusFilter, projectTagFilter]);

  useEffect(() => {
    if (!selectedProjectId) {
      startTransition(() => {
        setSelectedProjectData(null);
      });
      return;
    }

    void loadProjectWorkspace(selectedProjectId);
  }, [selectedProjectId]);

  useEffect(() => {
    if (!activeJobId) {
      return undefined;
    }

    const timer = window.setInterval(() => {
      pollActiveJob(activeJobId);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeJobId]);

  function canLeaveCurrentProjectContext(): boolean {
    if (!isProjectDirty) {
      return true;
    }

    return window.confirm(
      "編集中のプロジェクトがあります。変更を保存せずに別の文脈へ切り替えますか？",
    );
  }

  function switchSelectedProject(projectId: string, message?: string): boolean {
    if (projectId === selectedProjectId) {
      if (message) {
        setProjectMessage(message);
      }
      return true;
    }

    if (!canLeaveCurrentProjectContext()) {
      return false;
    }

    startTransition(() => {
      setSelectedProjectId(projectId);
    });

    if (message) {
      setProjectMessage(message);
    }

    return true;
  }

  async function submitPrompt(values: PromptFormSubmitValues): Promise<void> {
    setIsSubmitting(true);
    setErrorMessage(null);
    setStatusMessage("生成を受け付けています...");
    setAssetMessage(null);

    try {
      const created = await studioClient.createGenerationJob(
        values.mediaType,
        buildGeneratePayload(values, selectedProjectId || null),
      );

      setActiveJobId(created.job_id);
      setStatusMessage(`生成リクエストを受け付けました。ID: ${created.job_id}`);
      await loadJob(created.job_id);
    } catch (error) {
      setIsSubmitting(false);
      setErrorMessage(getErrorMessage(error, "生成の開始に失敗しました。"));
      setStatusMessage(null);
    }
  }

  function loadSelectedAssetIntoComposer(): void {
    if (!selectedAssetDetail) {
      return;
    }

    if (
      selectedAssetDetail.project_id &&
      selectedAssetDetail.project_id !== selectedProjectId &&
      !canLeaveCurrentProjectContext()
    ) {
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
      setSelectedProjectId(selectedAssetDetail.project_id ?? selectedProjectId);
      setMediaType(selectedAssetDetail.media_type);
      setComposerRevision((current) => current + 1);
      setAssetMessage(`素材 ${selectedAssetDetail.asset_id} を Composer に読み込みました。`);
    });
  }

  function resolveReusePrompt(
    assetDetail: GalleryAssetDetailResponse,
    currentPrompt: string,
  ): string {
    if (currentPrompt.trim().length > 0) {
      return currentPrompt;
    }

    if (assetDetail.request_snapshot.prompt.trim().length > 0) {
      return assetDetail.request_snapshot.prompt;
    }

    return assetDetail.prompt;
  }

  async function reuseSelectedAsset(): Promise<void> {
    if (!selectedAssetDetail) {
      return;
    }

    setIsAssetBusy(true);
    setErrorMessage(null);
    setAssetMessage(null);

    const currentDraft = mergeDraftWithDefaults(
      selectedAssetDetail.media_type,
      drafts[selectedAssetDetail.media_type],
    );
    const reusePrompt = resolveReusePrompt(selectedAssetDetail, currentDraft.prompt);
    const sourceValues =
      mediaType === selectedAssetDetail.media_type
        ? {
            ...currentDraft,
            prompt: reusePrompt,
          }
        : mergeDraftWithDefaults(selectedAssetDetail.media_type, {
            ...createDraftFromRequestSnapshot(selectedAssetDetail.request_snapshot),
            prompt: reusePrompt,
          });

    try {
      const payload = await studioClient.reuseAsset(
        selectedAssetDetail.asset_id,
        buildReusePayload(
          sourceValues,
          selectedProjectId || selectedAssetDetail.project_id || null,
        ),
      );

      startTransition(() => {
        setMediaType(selectedAssetDetail.media_type);
      });
      if (payload.project_id && payload.project_id !== selectedProjectId) {
        switchSelectedProject(payload.project_id);
      }
      setIsSubmitting(true);
      setActiveJobId(payload.job_id);
      setStatusMessage(`派生生成を受け付けました。ID: ${payload.job_id}`);
      setAssetMessage(`素材 ${selectedAssetDetail.asset_id} から派生生成を開始しました。`);
      await loadJob(payload.job_id);
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "選択中素材の再利用に失敗しました。"));
    } finally {
      setIsAssetBusy(false);
    }
  }

  async function exportSelectedAsset(): Promise<void> {
    if (!selectedAssetDetail) {
      return;
    }

    setIsAssetBusy(true);
    setErrorMessage(null);

    try {
      const payload = await studioClient.exportAsset(selectedAssetDetail.asset_id);
      setAssetMessage(`素材を書き出しました: ${payload.export_path}`);
      await refreshStudio(selectedAssetDetail.media_type, {
        preferredAssetId: selectedAssetDetail.asset_id,
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "素材の書き出しに失敗しました。"));
    } finally {
      setIsAssetBusy(false);
    }
  }

  async function updateSelectedAssetProject(): Promise<void> {
    if (!selectedAssetDetail) {
      return;
    }

    setIsAssetBusy(true);
    setErrorMessage(null);

    try {
      const payload = await studioClient.updateAssetProject(
        selectedAssetDetail.asset_id,
        selectedAssetProjectId || null,
      );
      const nextProjectId = payload.project_id ?? "";

      startTransition(() => {
        setSelectedAssetDetail(payload);
        setSelectedAssetId(payload.asset_id);
        setSelectedAssetProjectId(nextProjectId);
      });

      if (nextProjectId && nextProjectId !== selectedProjectId) {
        switchSelectedProject(nextProjectId);
      }

      setAssetMessage(
        payload.project_id
          ? `素材 ${payload.asset_id} を選択中プロジェクトへ割り当てました。`
          : `素材 ${payload.asset_id} のプロジェクト割り当てを解除しました。`,
      );

      await Promise.all([
        refreshStudio(payload.media_type, { preferredAssetId: payload.asset_id }),
        refreshProjectViews(nextProjectId || null),
      ]);
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "素材のプロジェクト割り当て更新に失敗しました。"));
    } finally {
      setIsAssetBusy(false);
    }
  }

  async function submitFeedback(): Promise<void> {
    if (!selectedAssetDetail) {
      return;
    }

    setIsFeedbackBusy(true);
    setErrorMessage(null);

    try {
      await studioClient.submitFeedback(buildFeedbackPayload(selectedAssetDetail, feedbackForm));
      startTransition(() => {
        setFeedbackForm(createEmptyFeedbackFormValues());
      });
      setAssetMessage(`素材 ${selectedAssetDetail.asset_id} にフィードバックを保存しました。`);
      await Promise.all([
        refreshStudio(selectedAssetDetail.media_type, {
          preferredAssetId: selectedAssetDetail.asset_id,
        }),
        refreshProjectViews(selectedAssetDetail.project_id ?? selectedProjectId ?? null),
        loadMetricsSummary(),
      ]);
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "フィードバックの保存に失敗しました。"));
    } finally {
      setIsFeedbackBusy(false);
    }
  }

  async function createProject(): Promise<void> {
    setIsProjectBusy(true);
    setErrorMessage(null);
    setProjectMessage(null);

    try {
      const payload = await studioClient.createProject(buildProjectPayload(createProjectForm));
      startTransition(() => {
        setCreateProjectForm(createEmptyProjectFormValues());
      });
      switchSelectedProject(payload.id);
      setProjectMessage(`「${payload.name}」を作成しました。このプロジェクトで生成できます。`);
      await refreshProjectViews(payload.id);
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "プロジェクトの作成に失敗しました。"));
    } finally {
      setIsProjectBusy(false);
    }
  }

  async function saveProject(values: ProjectFormValues): Promise<void> {
    if (!selectedProjectData) {
      return;
    }

    setIsProjectBusy(true);
    setErrorMessage(null);

    try {
      const payload = await studioClient.updateProject(
        selectedProjectData.project.id,
        buildProjectPayload(values),
      );
      setProjectMessage(`「${payload.name}」の変更を保存しました。`);
      await refreshProjectViews(payload.id);
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "プロジェクトの更新に失敗しました。"));
    } finally {
      setIsProjectBusy(false);
    }
  }

  async function exportProject(): Promise<void> {
    if (!selectedProjectData) {
      return;
    }

    setIsProjectBusy(true);
    setErrorMessage(null);

    try {
      const payload = await studioClient.exportProject(selectedProjectData.project.id);
      setProjectMessage(`プロジェクトを書き出しました。保存先: ${payload.bundle_root}`);
      await refreshProjectViews(selectedProjectData.project.id);
    } catch (error) {
      setErrorMessage(getErrorMessage(error, "プロジェクトの書き出しに失敗しました。"));
    } finally {
      setIsProjectBusy(false);
    }
  }

  return {
    state: {
      themeMode,
      setThemeMode,
      mediaType,
      setMediaType,
      composerRevision,
      loraOptions,
      drafts,
      setDrafts,
      controlModes,
      setControlModes,
      projectCatalog,
      projects,
      projectSearchText,
      setProjectSearchText,
      projectStatusFilter,
      setProjectStatusFilter,
      projectTagFilter,
      setProjectTagFilter,
      createProjectForm,
      setCreateProjectForm,
      selectedProjectId,
      selectedProjectData,
      selectedAssetId,
      selectedAssetDetail,
      selectedAssetProjectId,
      setSelectedAssetProjectId,
      feedbackForm,
      setFeedbackForm,
      isSubmitting,
      isAssetBusy,
      isFeedbackBusy,
      isProjectBusy,
      metricsSummary,
      statusMessage,
      assetMessage,
      projectMessage,
      errorMessage,
      isProjectDirty,
      setIsProjectDirty,
    },
    derived: {
      activeModels,
      activeControlMode,
      selectedProjectName,
      prioritizedGalleryItems,
      selectedProjectGalleryCount,
      reviewJob,
      availableStatuses,
      availableTags,
    },
    actions: {
      submitPrompt,
      selectAsset: loadAssetDetail,
      reuseSelectedAsset,
      loadSelectedAssetIntoComposer,
      exportSelectedAsset,
      updateSelectedAssetProject,
      submitFeedback,
      createProject,
      saveProject,
      exportProject,
      selectProject: (projectId: string) => {
        if (projectId === selectedProjectId) {
          return;
        }
        switchSelectedProject(projectId);
      },
      clearProjectSelection: () => {
        switchSelectedProject("", "プロジェクトの選択を解除しました。");
      },
      routeComposerToProject: () => {
        if (!selectedProjectData) {
          return;
        }

        switchSelectedProject(
          selectedProjectData.project.id,
          `以降の生成は「${selectedProjectData.project.name}」に紐づきます。`,
        );
      },
    },
  };
}
