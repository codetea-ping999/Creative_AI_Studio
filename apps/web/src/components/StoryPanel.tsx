import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  applyStoryResult,
  assembleStory,
  availableStages,
  createStory,
  generateSceneMedia,
  isReadyToAssemble,
  missingAudioCount,
  resolveSceneTarget,
  sceneRoleLabels,
  sceneScopedStages,
  expandStory,
  getStory,
  listStories,
  loglineCandidates,
  storyStages,
  updateStory,
  type SceneRole,
  type SceneVisualMediaType,
  type StoryDetail,
  type StoryDocument,
  type StoryScene,
  type StorySummary,
} from "../lib/storyApi";
import { useT } from "../i18n";
import { jobStatusLabel } from "../lib/jobStatus";

/** The scene role (and, for `visual`, how) a generation request is in flight for. */
type BusyScene = {
  sceneId: string;
  role: SceneRole;
  mediaType?: SceneVisualMediaType;
};

export type StoryPanelProps = {
  /** Text model to write with; empty falls back to the API default. */
  modelId: string;
  projectId?: string;
  /** Poll a job until it is terminal; returns the final status. */
  awaitJob: (jobId: string) => Promise<string>;
  /** Load one scene's visual brief into the image composer. */
  onGenerateSceneImage?: (scene: StoryScene) => void;
  /** Load one scene's narration into the audio composer. */
  onGenerateSceneNarration?: (scene: StoryScene) => void;
  /** Models used when generating a scene's media in place. */
  sceneModelIds?: Partial<Record<SceneRole, string>>;
};

type LoadState = "loading" | "ready" | "error";
type PendingStage = { task: string; jobId: string } | null;

const formatLabels: Record<string, string> = {
  "short-video": "ショート動画",
  novel: "小説",
  "picture-book": "絵本",
  ad: "広告",
};

const stageResultLabels: Record<string, string> = {
  logline: "ログライン",
  beat_sheet: "ビート",
  scene_list: "シーン",
  script: "脚本",
  prose: "本文",
};

function stageIsComplete(story: StoryDocument, task: string): boolean {
  if (task === "logline") return Boolean(story.logline.trim());
  if (task === "beat_sheet") return story.beats.length > 0;
  if (task === "scene_list") return story.scenes.length > 0;
  if (task === "script") {
    // Narration is written by the scene_list stage, so only dialogue proves the
    // script stage has run.
    return story.scenes.some((scene) => (scene.dialogue?.length ?? 0) > 0);
  }
  if (task === "prose") return story.chapters.length > 0;
  return false;
}

function StoryLoadingState({ label }: { label: string }) {
  return (
    <div className="surface-loading" role="status" aria-busy="true">
      <span className="surface-loading__bar" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

function StoryOutline({ story }: { story: StoryDocument }) {
  const t = useT();
  if (story.beats.length === 0) return null;
  return (
    <details className="story-disclosure">
      <summary>{t("構成ビート {count} 件", { count: story.beats.length })}</summary>
      <ol className="story-beat-list">
        {[...story.beats]
          .sort((left, right) => left.order - right.order)
          .map((beat) => (
            <li key={beat.id}>
              <span className="story-beat-list__index">{beat.act || "Beat"}</span>
              <div>
                <strong>{beat.purpose || t("目的未設定")}</strong>
                <p>{beat.summary || t("要約はまだありません。")}</p>
              </div>
            </li>
          ))}
      </ol>
    </details>
  );
}

function StoryScenes({
  detail,
  onGenerateSceneImage,
  onGenerateSceneNarration,
  onGenerateRole,
  busyScene,
}: {
  detail: StoryDetail;
  onGenerateSceneImage?: (scene: StoryScene) => void;
  onGenerateSceneNarration?: (scene: StoryScene) => void;
  onGenerateRole?: (
    scene: StoryScene,
    role: SceneRole,
    mediaType?: SceneVisualMediaType,
  ) => void;
  busyScene?: BusyScene | null;
}) {
  const t = useT();
  const scenes = [...detail.story.scenes].sort(
    (left, right) => left.order - right.order,
  );
  const totalDuration = scenes.reduce(
    (total, scene) => total + scene.duration_seconds,
    0,
  );
  const missingByScene = new Map<string, string[]>();
  for (const entry of detail.missing_assets) {
    missingByScene.set(entry.scene_id, [
      ...(missingByScene.get(entry.scene_id) ?? []),
      entry.role,
    ]);
  }

  if (scenes.length === 0) {
    return (
      <div className="empty-stage story-empty-stage">
        <div>
          <h3>{t("シーン表はまだありません")}</h3>
          <p>{t("シーンを生成すると、尺、ナレーション、素材の不足が並びます。")}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="story-scene-table__scroll">
      <table className="story-scene-table">
        <caption>
          {t("{count} シーン / {seconds} 秒", { count: scenes.length, seconds: totalDuration.toFixed(1) })}
          {detail.missing_assets.length > 0
            ? t(" — 不足素材 {count} 件", { count: detail.missing_assets.length })
            : t(" — 素材はすべて揃っています")}
        </caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">{t("シーン")}</th>
            <th scope="col">{t("尺 / BGM")}</th>
            <th scope="col">{t("素材")}</th>
            <th scope="col">{t("次の操作")}</th>
          </tr>
        </thead>
        <tbody>
          {scenes.map((scene, index) => {
            const missing = missingByScene.get(scene.id) ?? [];
            const assets = Object.entries(scene.asset_ids);
            return (
              <tr key={scene.id}>
                <td data-label={t("順番")}>{index + 1}</td>
                <td data-label={t("シーン")}>
                  <strong className="story-scene-title">
                    {scene.heading || scene.id}
                  </strong>
                  {scene.summary ? <p>{scene.summary}</p> : null}
                  {scene.narration ? (
                    <p className="story-scene-narration">
                      <span>{t("ナレーション")}</span>
                      {scene.narration}
                    </p>
                  ) : null}
                  {scene.dialogue && scene.dialogue.length > 0 ? (
                    <details className="story-scene-dialogue">
                      <summary>{t("台詞 {count} 行", { count: scene.dialogue.length })}</summary>
                      <ol>
                        {scene.dialogue.map((line, lineIndex) => (
                          <li key={`${scene.id}-${lineIndex}`}>
                            <strong>{line.speaker || t("話者未設定")}</strong>
                            {line.direction ? <em>（{line.direction}）</em> : null}
                            <span>{line.text}</span>
                          </li>
                        ))}
                      </ol>
                    </details>
                  ) : null}
                </td>
                <td data-label={t("尺 / BGM")}>
                  <span className="story-scene-duration">
                    {t("{seconds} 秒", { seconds: scene.duration_seconds.toFixed(1) })}
                  </span>
                  <span>{scene.bgm_mood || t("BGM 未設定")}</span>
                </td>
                <td data-label={t("素材")}>
                  {missing.length === 0 ? (
                    <span className="status-chip status-chip--succeeded">
                      {t("完備")}
                    </span>
                  ) : (
                    <span className="status-chip status-chip--queued">
                      {t("不足: {roles}", { roles: missing.map((role) => t(sceneRoleLabels[role as SceneRole] ?? role)).join(t("、")) })}
                    </span>
                  )}
                  {assets.length > 0 ? (
                    <details className="story-assets">
                      <summary>{t("紐づけ済み {count} 件", { count: assets.length })}</summary>
                      <dl>
                        {assets.map(([role, assetId]) => (
                          <div key={role}>
                            <dt>{role}</dt>
                            <dd>{assetId}</dd>
                          </div>
                        ))}
                      </dl>
                    </details>
                  ) : null}
                </td>
                <td data-label={t("次の操作")}>
                  <div className="story-scene-actions">
                    {onGenerateRole
                      ? (["visual", "narration", "music"] as SceneRole[]).map(
                          (role) => {
                            const isBusy =
                              busyScene?.sceneId === scene.id &&
                              busyScene.role === role &&
                              busyScene.mediaType !== "video";
                            const hasSource =
                              role === "visual"
                                ? Boolean(scene.image_prompt.trim())
                                : role === "narration"
                                  ? Boolean(scene.narration.trim())
                                  : Boolean(scene.bgm_mood.trim());
                            const isFilled = Boolean(scene.asset_ids[role]);
                            const isProceduralBusy =
                              busyScene?.sceneId === scene.id &&
                              busyScene.role === "visual" &&
                              busyScene.mediaType === "video";
                            return (
                              <Fragment key={role}>
                                <button
                                  type="button"
                                  className="secondary-button"
                                  onClick={() => onGenerateRole(scene, role)}
                                  disabled={!hasSource || Boolean(busyScene)}
                                  aria-busy={isBusy}
                                  title={
                                    hasSource
                                      ? isFilled
                                        ? t("{role}を作り直す", { role: t(sceneRoleLabels[role]) })
                                        : t("{role}を生成してこのシーンに紐付ける", { role: t(sceneRoleLabels[role]) })
                                      : t("{role}のもとになるテキストがありません", { role: t(sceneRoleLabels[role]) })
                                  }
                                >
                                  {isBusy
                                    ? t("{role}…", { role: t(sceneRoleLabels[role]) })
                                    : isFilled
                                      ? t("{role}を再生成", { role: t(sceneRoleLabels[role]) })
                                      : t("{role}を生成", { role: t(sceneRoleLabels[role]) })}
                                </button>
                                {role === "visual" ? (
                                  <button
                                    type="button"
                                    className="secondary-button"
                                    onClick={() =>
                                      onGenerateRole(scene, "visual", "video")
                                    }
                                    disabled={!hasSource || Boolean(busyScene)}
                                    aria-busy={isProceduralBusy}
                                    title={
                                      hasSource
                                        ? t("モデル不要の絵コンテ動画を生成して、このシーンに紐付ける")
                                        : t("ビジュアルのもとになる画像プロンプトがありません")
                                    }
                                  >
                                    {isProceduralBusy
                                      ? t("絵コンテ動画を生成中…")
                                      : t("絵コンテ動画を生成（モデル不要）")}
                                  </button>
                                ) : null}
                              </Fragment>
                            );
                          },
                        )
                      : null}
                    {onGenerateSceneImage ? (
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => onGenerateSceneImage(scene)}
                        disabled={!scene.image_prompt.trim()}
                        title={
                          scene.image_prompt
                            ? t("画像プロンプトを入力欄に読み込んで調整する")
                            : t("画像プロンプトがありません")
                        }
                      >
                        {t("画像を入力欄へ")}
                      </button>
                    ) : null}
                    {onGenerateSceneNarration ? (
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => onGenerateSceneNarration(scene)}
                        disabled={!scene.narration.trim()}
                        title={
                          scene.narration
                            ? t("ナレーションを音声の入力欄に読み込んで調整する")
                            : t("ナレーションがありません")
                        }
                      >
                        {t("音声を入力欄へ")}
                      </button>
                    ) : null}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function StoryPanel({
  modelId,
  projectId,
  awaitJob,
  onGenerateSceneImage,
  onGenerateSceneNarration,
  sceneModelIds,
}: StoryPanelProps) {
  const t = useT();
  const [stories, setStories] = useState<StorySummary[]>([]);
  const [formats, setFormats] = useState<string[]>(["short-video"]);
  const [listState, setListState] = useState<LoadState>("loading");
  const [selectedStoryId, setSelectedStoryId] = useState("");
  const [detail, setDetail] = useState<StoryDetail | null>(null);
  const [detailState, setDetailState] = useState<LoadState>("ready");
  const [title, setTitle] = useState("");
  const [premise, setPremise] = useState("");
  const [storyFormat, setStoryFormat] = useState("short-video");
  const [genre, setGenre] = useState("");
  const [tone, setTone] = useState("");
  const [characters, setCharacters] = useState("");
  const [editPremise, setEditPremise] = useState("");
  const [editLogline, setEditLogline] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [pending, setPending] = useState<PendingStage>(null);
  const [busyScene, setBusyScene] = useState<BusyScene | null>(null);
  const [isAssembling, setIsAssembling] = useState(false);
  const [scriptSceneId, setScriptSceneId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refreshStories = useCallback(async () => {
    setListState("loading");
    try {
      const response = await listStories({ projectId, limit: 100 });
      setStories(response.items);
      setFormats(response.formats?.length ? response.formats : ["short-video"]);
      setListState("ready");
      setError("");
      return response.items;
    } catch (cause) {
      setListState("error");
      setError(cause instanceof Error ? cause.message : String(cause));
      return [];
    }
  }, [projectId]);

  useEffect(() => {
    setSelectedStoryId("");
    setDetail(null);
    void refreshStories();
  }, [refreshStories]);

  const loadDetail = useCallback(async (storyId: string) => {
    if (!storyId) {
      setDetail(null);
      setDetailState("ready");
      return;
    }
    setDetail(null);
    setDetailState("loading");
    try {
      const response = await getStory(storyId);
      setDetail(response);
      setDetailState("ready");
      setError("");
    } catch (cause) {
      setDetailState("error");
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void loadDetail(selectedStoryId);
  }, [loadDetail, selectedStoryId]);

  const story = detail?.story ?? null;
  const stages = useMemo(() => availableStages(story), [story]);
  const candidates = useMemo(() => loglineCandidates(story), [story]);
  const totalDuration = useMemo(
    () =>
      story?.scenes.reduce(
        (total, scene) => total + scene.duration_seconds,
        0,
      ) ?? 0,
    [story],
  );

  const orderedScenes = useMemo(
    () => [...(story?.scenes ?? [])].sort((left, right) => left.order - right.order),
    [story],
  );
  const scriptTargetId = resolveSceneTarget(story, scriptSceneId);
  const scriptTarget =
    orderedScenes.find((scene) => scene.id === scriptTargetId) ?? null;

  useEffect(() => {
    setEditPremise(story?.premise ?? "");
    setEditLogline(story?.logline ?? "");
  }, [story]);

  async function handleCreate() {
    if (!premise.trim() && !title.trim()) return;
    setError("");
    setNotice("");
    setIsCreating(true);
    try {
      const created = await createStory({
        title: title.trim() || premise.trim().slice(0, 40) || t("無題のストーリー"),
        premise: premise.trim(),
        project_id: projectId,
        format: storyFormat,
        genre: genre.trim(),
        tone: tone.trim(),
        characters: characters
          .split(/[,、\n]/)
          .map((entry) => entry.trim())
          .filter(Boolean),
      });
      setTitle("");
      setPremise("");
      setGenre("");
      setTone("");
      setCharacters("");
      await refreshStories();
      setSelectedStoryId(created.id);
      setNotice(t("ストーリーを作成しました。次はログラインを生成できます。"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setIsCreating(false);
    }
  }

  async function handleGenerateRole(
    scene: StoryScene,
    role: SceneRole,
    mediaType?: SceneVisualMediaType,
  ) {
    if (!story) return;
    const label =
      mediaType === "video" ? t("絵コンテ動画") : t(sceneRoleLabels[role]);
    setError("");
    setNotice("");
    setBusyScene({ sceneId: scene.id, role, mediaType });
    try {
      const { job_id: jobId } = await generateSceneMedia(story.id, scene.id, {
        role,
        // The procedural clip always uses the server's Stable model default; a
        // still-image model choice must not leak into it.
        model_id: mediaType === "video" ? "" : (sceneModelIds?.[role] ?? ""),
        ...(mediaType ? { media_type: mediaType } : {}),
      });
      const status = await awaitJob(jobId);
      // The server binds the finished asset to the scene, so the panel only has
      // to re-read the story to see it.
      setDetail(await getStory(story.id));
      setNotice(
        status === "succeeded"
          ? t("{label}を {scene} に紐付けました。", { label, scene: scene.heading || scene.id })
          : t("{label}の生成が「{status}」で終了しました。", { label, status: t(jobStatusLabel(status)) }),
      );
      if (status !== "succeeded") {
        setError(t("{label}の生成に失敗しました（{status}）。", { label, status: t(jobStatusLabel(status)) }));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusyScene(null);
    }
  }

  async function handleAssemble() {
    if (!story) return;
    setError("");
    setNotice("");
    setIsAssembling(true);
    try {
      const { job_id: jobId } = await assembleStory(story.id);
      const status = await awaitJob(jobId);
      setNotice(
        status === "succeeded"
          ? t("動画を書き出しました。ギャラリーで確認できます。")
          : t("書き出しが「{status}」で終了しました。", { status: t(jobStatusLabel(status)) }),
      );
      if (status !== "succeeded") {
        setError(t("書き出しに失敗しました（{status}）。", { status: t(jobStatusLabel(status)) }));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setIsAssembling(false);
    }
  }

  async function handleExpand(task: string) {
    if (!story || pending) return;
    if (sceneScopedStages.has(task) && !scriptTargetId) {
      setError(t("台詞を書くシーンがありません。先にシーンを生成してください。"));
      return;
    }
    setError("");
    setNotice("");
    try {
      const { job_id: jobId } = await expandStory(story.id, {
        task,
        model_id: modelId,
        params: sceneScopedStages.has(task)
          ? { scene_id: scriptTargetId }
          : task === "scene_list"
            ? { scene_count: 5 }
            : {},
      });
      setPending({ task, jobId });
      const status = await awaitJob(jobId);
      if (status !== "succeeded") {
        setError(
          t("{stage}の生成は「{status}」で終了しました。入力は保持されています。", { stage: t(stageResultLabels[task] ?? task), status: t(jobStatusLabel(status)) }),
        );
        return;
      }
      const nextDetail = await applyStoryResult(story.id, jobId);
      setDetail(nextDetail);
      setNotice(
        sceneScopedStages.has(task)
          ? t("{stage}を「{scene}」へ反映しました。", {
              stage: t(stageResultLabels[task] ?? task),
              scene: scriptTarget?.heading || scriptTargetId,
            })
          : t("{stage}をストーリーへ反映しました。", { stage: t(stageResultLabels[task] ?? task) }),
      );
      await refreshStories();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPending(null);
    }
  }

  async function saveStoryBrief(logline = editLogline) {
    if (!story) return;
    setIsSaving(true);
    setError("");
    try {
      await updateStory(story.id, {
        premise: editPremise.trim(),
        logline: logline.trim(),
      });
      await loadDetail(story.id);
      await refreshStories();
      setNotice(t("前提とログラインを保存しました。"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="section-card story-panel" aria-labelledby="story-panel-heading">
      <div className="section-card__header story-panel__header">
        <div>
          <p className="eyebrow">{t("ストーリー")}</p>
          <h2 id="story-panel-heading">{t("構想から絵コンテまで")}</h2>
        </div>
        <p className="section-footnote">
          {t("前提を段階的に展開し、各シーンを画像・音声制作へ渡します。")}
        </p>
      </div>

      {error ? (
        <div className="error-banner story-error" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="secondary-button"
            onClick={() =>
              void (selectedStoryId
                ? loadDetail(selectedStoryId)
                : refreshStories())
            }
          >
            {t("再試行")}
          </button>
        </div>
      ) : null}
      <p className="story-notice" role="status" aria-live="polite">
        {notice}
      </p>

      <fieldset className="story-create">
        <legend>{t("新しいストーリー")}</legend>
        <div className="field-grid">
          <label className="field-group">
            <span>{t("タイトル")}</span>
            <input
              type="text"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder={t("例: Rewind")}
              disabled={isCreating}
            />
          </label>
          <label className="field-group">
            <span>{t("形式")}</span>
            <select
              value={storyFormat}
              onChange={(event) => setStoryFormat(event.target.value)}
              disabled={isCreating}
            >
              {formats.map((entry) => (
                <option key={entry} value={entry}>
                  {t(formatLabels[entry] ?? entry)}
                </option>
              ))}
            </select>
          </label>
          <label className="field-group field-group--full">
            <span>{t("前提（premise）")}</span>
            <textarea
              rows={3}
              value={premise}
              onChange={(event) => setPremise(event.target.value)}
              placeholder={t("例: 時を巻き戻せる少女が、最後の一日を選び直す")}
              disabled={isCreating}
              aria-describedby="story-create-help"
            />
          </label>
        </div>
        <details className="story-disclosure story-create__details">
          <summary>{t("ジャンル・トーン・登場人物を設定")}</summary>
          <div className="field-grid">
            <label className="field-group">
              <span>{t("ジャンル")}</span>
              <input
                value={genre}
                onChange={(event) => setGenre(event.target.value)}
                placeholder={t("例: SF ドラマ")}
                disabled={isCreating}
              />
            </label>
            <label className="field-group">
              <span>{t("トーン")}</span>
              <input
                value={tone}
                onChange={(event) => setTone(event.target.value)}
                placeholder={t("例: 静かで切ない")}
                disabled={isCreating}
              />
            </label>
            <label className="field-group field-group--full">
              <span>{t("登場人物（読点または改行区切り）")}</span>
              <textarea
                rows={2}
                value={characters}
                onChange={(event) => setCharacters(event.target.value)}
                placeholder={t("ユイ、祖父、駅員")}
                disabled={isCreating}
              />
            </label>
          </div>
        </details>
        <div className="form-actions story-create__actions">
          <button
            type="button"
            onClick={() => void handleCreate()}
            disabled={isCreating || (!premise.trim() && !title.trim())}
            aria-busy={isCreating}
            aria-describedby="story-create-help"
          >
            {isCreating ? t("作成中…") : t("ストーリーを作成")}
          </button>
          <span id="story-create-help">
            {!premise.trim() && !title.trim()
              ? t("タイトルまたは前提を入力してください。")
              : t("{format}として保存します。", { format: t(formatLabels[storyFormat] ?? storyFormat) })}
          </span>
        </div>
      </fieldset>

      {listState === "loading" ? (
        <StoryLoadingState label={t("ストーリー一覧を読み込んでいます…")} />
      ) : (
        <label className="field-group field-group--full">
          <span>{t("編集中のストーリー")}</span>
          <select
            value={selectedStoryId}
            onChange={(event) => setSelectedStoryId(event.target.value)}
            disabled={listState === "error" || stories.length === 0}
          >
            <option value="">
              {stories.length === 0 ? t("保存済みストーリーはありません") : t("選択してください")}
            </option>
            {stories.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.title || entry.id} — {entry.scene_count} scenes
              </option>
            ))}
          </select>
        </label>
      )}

      {!selectedStoryId && listState === "ready" ? (
        <div className="empty-stage story-empty-stage">
          <div>
            <h3>{t("執筆を始める準備ができています")}</h3>
            <p>
              {t("ストーリーを作成するか、保存済みストーリーを選ぶと、段階生成とシーン表が表示されます。")}
            </p>
          </div>
        </div>
      ) : detailState === "loading" ? (
        <StoryLoadingState label={t("ストーリー本文を読み込んでいます…")} />
      ) : story && detail ? (
        <div className="story-workspace">
          <div className="story-workspace__identity">
            <div>
              <span>{t(formatLabels[story.format] ?? story.format)}</span>
              <h3>{story.title || t("無題のストーリー")}</h3>
            </div>
            <span className="story-id">{story.id}</span>
          </div>

          {pending ? (
            <p className="story-running" role="status" aria-live="polite">
              <span className="status-chip status-chip--running">{t("生成中")}</span>
              {t("{stage}を執筆しています。完了後に自動反映します。", { stage: t(stageResultLabels[pending.task] ?? pending.task) })}
            </p>
          ) : null}

          <ol className="story-stage-list" aria-label={t("執筆段階")}>
            {storyStages.map((stage, index) => {
              const isPending = pending?.task === stage.task;
              const isComplete = stageIsComplete(story, stage.task);
              const needsScene = sceneScopedStages.has(stage.task);
              const isAvailable =
                stages.has(stage.task) && (!needsScene || Boolean(scriptTargetId));
              const isDisabled = !isAvailable || pending !== null;
              const stateLabel = isPending
                ? t("生成中")
                : isComplete
                  ? t("完了・再生成可能")
                  : isAvailable
                    ? needsScene
                      ? t("対象 {scene}", { scene: scriptTarget?.heading || scriptTargetId })
                      : t("生成可能")
                    : t("前段階の完了待ち");
              return (
                <li
                  key={stage.task}
                  className={isComplete ? "is-complete" : isAvailable ? "is-ready" : ""}
                >
                  <span className="story-stage-list__index" aria-hidden="true">
                    {index + 1}
                  </span>
                  <div className="story-stage-list__copy">
                    <strong>{t(stage.label)}</strong>
                    <span id={`story-stage-${stage.task}-hint`}>
                      {t(stage.hint)} — {stateLabel}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => void handleExpand(stage.task)}
                    disabled={isDisabled}
                    aria-busy={isPending}
                    aria-describedby={`story-stage-${stage.task}-hint`}
                  >
                    {isPending ? t("{role}…", { role: t(stage.label) }) : t(stage.label)}
                  </button>
                </li>
              );
            })}
          </ol>

          {orderedScenes.length > 0 ? (
            <div className="story-script-target">
              <label className="field-group field-group--full">
                <span>{t("台本の対象シーン")}</span>
                <select
                  value={scriptTargetId}
                  onChange={(event) => setScriptSceneId(event.target.value)}
                  disabled={pending !== null}
                  aria-describedby="story-script-target-help"
                >
                  {orderedScenes.map((scene, index) => (
                    <option key={scene.id} value={scene.id}>
                      {index + 1}. {scene.heading || scene.id}
                      {(scene.dialogue?.length ?? 0) > 0 ? t("（台詞あり）") : ""}
                    </option>
                  ))}
                </select>
              </label>
              <p id="story-script-target-help" className="section-footnote">
                {t("台本はこのシーンにだけ台詞を書き込みます。台詞は動画には合成されず、 シーンの台本として保存されます。")}
              </p>
            </div>
          ) : null}

          <dl className="story-summary" aria-label={t("ストーリーの進捗")}>
            <div>
              <dt>{t("ログライン")}</dt>
              <dd>{story.logline || t("未生成")}</dd>
            </div>
            <div>
              <dt>{t("構成")}</dt>
              <dd>{story.beats.length}</dd>
            </div>
            <div>
              <dt>{t("シーン")}</dt>
              <dd>{story.scenes.length}</dd>
            </div>
            <div>
              <dt>{t("合計尺")}</dt>
              <dd>{t("{seconds} 秒", { seconds: totalDuration.toFixed(1) })}</dd>
            </div>
          </dl>

          <details className="story-disclosure story-brief-editor">
            <summary>{t("前提とログラインを手直し")}</summary>
            <div className="field-grid">
              <label className="field-group field-group--full">
                <span>{t("前提")}</span>
                <textarea
                  value={editPremise}
                  onChange={(event) => setEditPremise(event.target.value)}
                  disabled={isSaving || pending !== null}
                />
              </label>
              <label className="field-group field-group--full">
                <span>{t("ログライン")}</span>
                <textarea
                  value={editLogline}
                  onChange={(event) => setEditLogline(event.target.value)}
                  disabled={isSaving || pending !== null}
                />
              </label>
            </div>
            <div className="form-actions">
              <button
                type="button"
                onClick={() => void saveStoryBrief()}
                disabled={isSaving || pending !== null}
                aria-busy={isSaving}
              >
                {isSaving ? t("保存中…") : t("手直しを保存")}
              </button>
            </div>
          </details>

          {candidates.length > 0 ? (
            <details className="story-disclosure" open>
              <summary>{t("ログライン候補 {count} 件", { count: candidates.length })}</summary>
              <ol className="story-candidate-list">
                {candidates.map((candidate, index) => (
                  <li key={`${index}-${candidate.slice(0, 24)}`}>
                    <p>{candidate}</p>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => {
                        setEditLogline(candidate);
                        void saveStoryBrief(candidate);
                      }}
                      disabled={isSaving || pending !== null || candidate === story.logline}
                    >
                      {candidate === story.logline ? t("採用中") : t("この案を採用")}
                    </button>
                  </li>
                ))}
              </ol>
            </details>
          ) : null}

          <StoryOutline story={story} />
          <StoryScenes
            detail={detail}
            onGenerateSceneImage={onGenerateSceneImage}
            onGenerateSceneNarration={onGenerateSceneNarration}
            onGenerateRole={(scene, role, mediaType) => {
              void handleGenerateRole(scene, role, mediaType);
            }}
            busyScene={busyScene}
          />

          <div className="story-assemble-row">
            <button
              type="button"
              className="secondary-button"
              onClick={() => void handleAssemble()}
              disabled={!isReadyToAssemble(detail) || isAssembling}
              aria-busy={isAssembling}
              aria-describedby="story-assemble-help"
              title={
                isReadyToAssemble(detail)
                  ? t("シーンを 1 本の MP4 に書き出す")
                  : t("すべてのシーンにビジュアルが揃うと書き出せます")
              }
            >
              {isAssembling ? t("書き出し中…") : t("動画を書き出す")}
            </button>
            <p id="story-assemble-help" className="section-footnote">
              {isReadyToAssemble(detail)
                ? t("{count} シーン / {seconds} 秒を 1920x1080 で書き出します。", {
                    count: detail.story.scenes.length,
                    seconds: detail.story.scenes
                      .reduce((total, scene) => total + scene.duration_seconds, 0)
                      .toFixed(1),
                  }) +
                  (missingAudioCount(detail) > 0
                    ? t("ナレーション/BGM が {count} 件未生成のため、その部分は無音になります。", {
                        count: missingAudioCount(detail),
                      })
                    : "")
                : t("ビジュアルが {count} 件不足しています。", {
                    count: detail.missing_assets.filter((entry) => entry.role === "visual").length,
                  })}
            </p>
          </div>

          {story.chapters.length > 0 ? (
            <details className="story-disclosure">
              <summary>{t("本文 / 章 {count} 件", { count: story.chapters.length })}</summary>
              <ol className="story-chapter-list">
                {[...story.chapters]
                  .sort((left, right) => (left.order ?? 0) - (right.order ?? 0))
                  .map((chapter) => (
                    <li key={chapter.id}>
                      <strong>{chapter.title || chapter.id}</strong>
                      <span>{chapter.word_count.toLocaleString()} words</span>
                      {chapter.prose_markdown ? <p>{chapter.prose_markdown}</p> : null}
                    </li>
                  ))}
              </ol>
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
