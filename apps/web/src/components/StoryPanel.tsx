import { useCallback, useEffect, useMemo, useState } from "react";
import {
  applyStoryResult,
  availableStages,
  createStory,
  expandStory,
  getStory,
  listStories,
  loglineCandidates,
  storyStages,
  updateStory,
  type StoryDetail,
  type StoryDocument,
  type StoryScene,
  type StorySummary,
} from "../lib/storyApi";

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
    return story.scenes.some(
      (scene) => scene.narration.trim() || (scene.dialogue?.length ?? 0) > 0,
    );
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
  if (story.beats.length === 0) return null;
  return (
    <details className="story-disclosure">
      <summary>構成ビート {story.beats.length} 件</summary>
      <ol className="story-beat-list">
        {[...story.beats]
          .sort((left, right) => left.order - right.order)
          .map((beat) => (
            <li key={beat.id}>
              <span className="story-beat-list__index">{beat.act || "Beat"}</span>
              <div>
                <strong>{beat.purpose || "目的未設定"}</strong>
                <p>{beat.summary || "要約はまだありません。"}</p>
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
}: {
  detail: StoryDetail;
  onGenerateSceneImage?: (scene: StoryScene) => void;
  onGenerateSceneNarration?: (scene: StoryScene) => void;
}) {
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
          <h3>シーン表はまだありません</h3>
          <p>Scenes を生成すると、尺、ナレーション、素材の不足が並びます。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="story-scene-table__scroll">
      <table className="story-scene-table">
        <caption>
          {scenes.length} シーン / {totalDuration.toFixed(1)} 秒
          {detail.missing_assets.length > 0
            ? ` — 不足素材 ${detail.missing_assets.length} 件`
            : " — 素材はすべて揃っています"}
        </caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">シーン</th>
            <th scope="col">尺 / BGM</th>
            <th scope="col">素材</th>
            <th scope="col">次の操作</th>
          </tr>
        </thead>
        <tbody>
          {scenes.map((scene, index) => {
            const missing = missingByScene.get(scene.id) ?? [];
            const assets = Object.entries(scene.asset_ids);
            return (
              <tr key={scene.id}>
                <td data-label="順番">{index + 1}</td>
                <td data-label="シーン">
                  <strong className="story-scene-title">
                    {scene.heading || scene.id}
                  </strong>
                  {scene.summary ? <p>{scene.summary}</p> : null}
                  {scene.narration ? (
                    <p className="story-scene-narration">
                      <span>ナレーション</span>
                      {scene.narration}
                    </p>
                  ) : null}
                </td>
                <td data-label="尺 / BGM">
                  <span className="story-scene-duration">
                    {scene.duration_seconds.toFixed(1)} 秒
                  </span>
                  <span>{scene.bgm_mood || "BGM 未設定"}</span>
                </td>
                <td data-label="素材">
                  {missing.length === 0 ? (
                    <span className="status-chip status-chip--succeeded">
                      完備
                    </span>
                  ) : (
                    <span className="status-chip status-chip--queued">
                      不足: {missing.join(", ")}
                    </span>
                  )}
                  {assets.length > 0 ? (
                    <details className="story-assets">
                      <summary>紐づけ済み {assets.length} 件</summary>
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
                <td data-label="次の操作">
                  <div className="story-scene-actions">
                    {onGenerateSceneImage ? (
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => onGenerateSceneImage(scene)}
                        disabled={!scene.image_prompt.trim()}
                        title={
                          scene.image_prompt
                            ? "画像プロンプトをコンポーザに読み込む"
                            : "画像プロンプトがありません"
                        }
                      >
                        画像を生成
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
                            ? "ナレーションを音声コンポーザに読み込む"
                            : "ナレーションがありません"
                        }
                      >
                        音声を生成
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
}: StoryPanelProps) {
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
        title: title.trim() || premise.trim().slice(0, 40) || "Untitled story",
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
      setNotice("ストーリーを作成しました。次は Logline を生成できます。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setIsCreating(false);
    }
  }

  async function handleExpand(task: string) {
    if (!story || pending) return;
    setError("");
    setNotice("");
    try {
      const { job_id: jobId } = await expandStory(story.id, {
        task,
        model_id: modelId,
        params: task === "scene_list" ? { scene_count: 5 } : {},
      });
      setPending({ task, jobId });
      const status = await awaitJob(jobId);
      if (status !== "succeeded") {
        setError(
          `${stageResultLabels[task] ?? task}の生成は「${status}」で終了しました。入力は保持されています。`,
        );
        return;
      }
      const nextDetail = await applyStoryResult(story.id, jobId);
      setDetail(nextDetail);
      setNotice(`${stageResultLabels[task] ?? task}をストーリーへ反映しました。`);
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
      setNotice("前提とログラインを保存しました。");
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
          <p className="eyebrow">Story</p>
          <h2 id="story-panel-heading">構想から絵コンテまで</h2>
        </div>
        <p className="section-footnote">
          前提を段階的に展開し、各シーンを画像・音声制作へ渡します。
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
            再試行
          </button>
        </div>
      ) : null}
      <p className="story-notice" role="status" aria-live="polite">
        {notice}
      </p>

      <fieldset className="story-create">
        <legend>新しいストーリー</legend>
        <div className="field-grid">
          <label className="field-group">
            <span>タイトル</span>
            <input
              type="text"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例: Rewind"
              disabled={isCreating}
            />
          </label>
          <label className="field-group">
            <span>形式</span>
            <select
              value={storyFormat}
              onChange={(event) => setStoryFormat(event.target.value)}
              disabled={isCreating}
            >
              {formats.map((entry) => (
                <option key={entry} value={entry}>
                  {formatLabels[entry] ?? entry}
                </option>
              ))}
            </select>
          </label>
          <label className="field-group field-group--full">
            <span>前提（premise）</span>
            <textarea
              rows={3}
              value={premise}
              onChange={(event) => setPremise(event.target.value)}
              placeholder="例: 時を巻き戻せる少女が、最後の一日を選び直す"
              disabled={isCreating}
              aria-describedby="story-create-help"
            />
          </label>
        </div>
        <details className="story-disclosure story-create__details">
          <summary>ジャンル・トーン・登場人物を設定</summary>
          <div className="field-grid">
            <label className="field-group">
              <span>ジャンル</span>
              <input
                value={genre}
                onChange={(event) => setGenre(event.target.value)}
                placeholder="例: SF ドラマ"
                disabled={isCreating}
              />
            </label>
            <label className="field-group">
              <span>トーン</span>
              <input
                value={tone}
                onChange={(event) => setTone(event.target.value)}
                placeholder="例: 静かで切ない"
                disabled={isCreating}
              />
            </label>
            <label className="field-group field-group--full">
              <span>登場人物（読点または改行区切り）</span>
              <textarea
                rows={2}
                value={characters}
                onChange={(event) => setCharacters(event.target.value)}
                placeholder="ユイ、祖父、駅員"
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
            {isCreating ? "作成中…" : "ストーリーを作成"}
          </button>
          <span id="story-create-help">
            {!premise.trim() && !title.trim()
              ? "タイトルまたは前提を入力してください。"
              : `${formatLabels[storyFormat] ?? storyFormat}として保存します。`}
          </span>
        </div>
      </fieldset>

      {listState === "loading" ? (
        <StoryLoadingState label="ストーリー一覧を読み込んでいます…" />
      ) : (
        <label className="field-group field-group--full">
          <span>編集中のストーリー</span>
          <select
            value={selectedStoryId}
            onChange={(event) => setSelectedStoryId(event.target.value)}
            disabled={listState === "error" || stories.length === 0}
          >
            <option value="">
              {stories.length === 0 ? "保存済みストーリーはありません" : "選択してください"}
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
            <h3>執筆を始める準備ができています</h3>
            <p>
              ストーリーを作成するか、保存済みストーリーを選ぶと、段階生成とシーン表が表示されます。
            </p>
          </div>
        </div>
      ) : detailState === "loading" ? (
        <StoryLoadingState label="ストーリー本文を読み込んでいます…" />
      ) : story && detail ? (
        <div className="story-workspace">
          <div className="story-workspace__identity">
            <div>
              <span>{formatLabels[story.format] ?? story.format}</span>
              <h3>{story.title || "無題のストーリー"}</h3>
            </div>
            <span className="story-id">{story.id}</span>
          </div>

          {pending ? (
            <p className="story-running" role="status" aria-live="polite">
              <span className="status-chip status-chip--running">生成中</span>
              {stageResultLabels[pending.task] ?? pending.task}を執筆しています。完了後に自動反映します。
            </p>
          ) : null}

          <ol className="story-stage-list" aria-label="執筆段階">
            {storyStages.map((stage, index) => {
              const isPending = pending?.task === stage.task;
              const isComplete = stageIsComplete(story, stage.task);
              const isAvailable = stages.has(stage.task);
              const isDisabled = !isAvailable || pending !== null;
              const stateLabel = isPending
                ? "生成中"
                : isComplete
                  ? "完了・再生成可能"
                  : isAvailable
                    ? "生成可能"
                    : "前段階の完了待ち";
              return (
                <li
                  key={stage.task}
                  className={isComplete ? "is-complete" : isAvailable ? "is-ready" : ""}
                >
                  <span className="story-stage-list__index" aria-hidden="true">
                    {index + 1}
                  </span>
                  <div className="story-stage-list__copy">
                    <strong>{stage.label}</strong>
                    <span id={`story-stage-${stage.task}-hint`}>
                      {stage.hint} — {stateLabel}
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
                    {isPending ? `${stage.label}…` : stage.label}
                  </button>
                </li>
              );
            })}
          </ol>

          <dl className="story-summary" aria-label="ストーリーの進捗">
            <div>
              <dt>Logline</dt>
              <dd>{story.logline || "未生成"}</dd>
            </div>
            <div>
              <dt>Beats</dt>
              <dd>{story.beats.length}</dd>
            </div>
            <div>
              <dt>Scenes</dt>
              <dd>{story.scenes.length}</dd>
            </div>
            <div>
              <dt>合計尺</dt>
              <dd>{totalDuration.toFixed(1)} 秒</dd>
            </div>
          </dl>

          <details className="story-disclosure story-brief-editor">
            <summary>前提とログラインを手直し</summary>
            <div className="field-grid">
              <label className="field-group field-group--full">
                <span>前提</span>
                <textarea
                  value={editPremise}
                  onChange={(event) => setEditPremise(event.target.value)}
                  disabled={isSaving || pending !== null}
                />
              </label>
              <label className="field-group field-group--full">
                <span>ログライン</span>
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
                {isSaving ? "保存中…" : "手直しを保存"}
              </button>
            </div>
          </details>

          {candidates.length > 0 ? (
            <details className="story-disclosure" open>
              <summary>Logline 候補 {candidates.length} 件</summary>
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
                      {candidate === story.logline ? "採用中" : "この案を採用"}
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
          />

          {story.chapters.length > 0 ? (
            <details className="story-disclosure">
              <summary>本文 / 章 {story.chapters.length} 件</summary>
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
