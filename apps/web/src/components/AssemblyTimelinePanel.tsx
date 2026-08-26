import { useCallback, useEffect, useState } from "react";
import {
  getStory,
  listStories,
  sceneRoleLabels,
  type SceneRole,
  type StoryDetail,
  type StoryScene,
  type StorySummary,
} from "../lib/storyApi";

export type AssemblyTimelinePanelProps = {
  projectId?: string;
};

type LoadState = "loading" | "ready" | "error";

/** Roles are always shown in production order: what you see, then what you hear. */
const roleOrder: SceneRole[] = ["visual", "narration", "music"];

/**
 * Whether one scene/role pair is filled, still needed, or genuinely optional.
 *
 * `missing` only ever applies to roles the story API flagged in
 * `missing_assets` — a silent establishing shot with no narration is a valid
 * scene, not a defect, so it renders as `optional` rather than `missing`.
 */
type RoleStatus = "assigned" | "missing" | "optional";

const roleStatusLabels: Record<RoleStatus, string> = {
  assigned: "割り当て済み",
  missing: "未割り当て",
  optional: "未設定（任意）",
};

// A glyph carries the state on its own so the badge still reads correctly for
// color-blind users or in a high-contrast/no-color theme — text alone would
// also work, but the glyph gives a scannable column when scenes stack up.
const roleStatusGlyphs: Record<RoleStatus, string> = {
  assigned: "✓",
  missing: "!",
  optional: "–",
};

function formatSeconds(value: number): string {
  return `${value.toFixed(1)} 秒`;
}

function roleStatusOf(
  scene: StoryScene,
  role: SceneRole,
  missingRoles: Set<SceneRole>,
): RoleStatus {
  if (scene.asset_ids[role]) {
    return "assigned";
  }
  return missingRoles.has(role) ? "missing" : "optional";
}

function AssemblyTimelineLoading({ label }: { label: string }) {
  return (
    <div className="surface-loading" role="status" aria-busy="true">
      <span className="surface-loading__bar" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/**
 * Scene-order table: order, duration (with a running start/end so the
 * "timeline" framing is literal), and every role's assignment state.
 *
 * Read-only by design (issue #244): no generation actions, no export
 * controls, no reordering. Those are later, separate microtasks of #62.
 */
function AssemblyTimelineScenes({ detail }: { detail: StoryDetail }) {
  const scenes = [...detail.story.scenes].sort((left, right) => left.order - right.order);
  const totalDuration = scenes.reduce((total, scene) => total + scene.duration_seconds, 0);

  const missingByScene = new Map<string, Set<SceneRole>>();
  for (const entry of detail.missing_assets) {
    const roles = missingByScene.get(entry.scene_id) ?? new Set<SceneRole>();
    roles.add(entry.role as SceneRole);
    missingByScene.set(entry.scene_id, roles);
  }

  if (scenes.length === 0) {
    return (
      <div className="empty-stage story-empty-stage">
        <div>
          <h3>並べるシーンがありません</h3>
          <p>
            Story パネルで Scenes を生成すると、順番・尺・素材の割り当てがここに並びます。
          </p>
        </div>
      </div>
    );
  }

  // Each row's start is the sum of every earlier scene's duration. Scene
  // counts are small (dozens, not thousands), so the O(n^2) sum reads more
  // plainly than a running accumulator and stays a pure derivation with
  // nothing mutated across renders.
  const rows = scenes.map((scene, index) => {
    const startSeconds = scenes
      .slice(0, index)
      .reduce((sum, earlier) => sum + earlier.duration_seconds, 0);
    return { scene, startSeconds, endSeconds: startSeconds + scene.duration_seconds };
  });

  return (
    <div className="story-scene-table__scroll assembly-timeline-scroll">
      <table className="story-scene-table assembly-timeline-table">
        <caption>
          {scenes.length} シーン / 合計 {formatSeconds(totalDuration)}
          {detail.missing_assets.length > 0
            ? ` — 不足素材 ${detail.missing_assets.length} 件`
            : " — 素材はすべて揃っています"}
        </caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">シーン</th>
            <th scope="col">開始 / 尺</th>
            <th scope="col">素材の割り当て</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ scene, startSeconds, endSeconds }, index) => {
            const missingRoles = missingByScene.get(scene.id) ?? new Set<SceneRole>();
            return (
              <tr key={scene.id}>
                <td data-label="順番">{index + 1}</td>
                <td data-label="シーン">
                  <strong className="story-scene-title">{scene.heading || scene.id}</strong>
                  {scene.narration ? (
                    <p className="story-scene-narration">
                      <span>ナレーション</span>
                      {scene.narration}
                    </p>
                  ) : (
                    <p>ナレーションなし（無音のカット）</p>
                  )}
                </td>
                <td data-label="開始 / 尺">
                  <span className="story-scene-duration">
                    {startSeconds.toFixed(1)}s → {endSeconds.toFixed(1)}s
                  </span>
                  <span>{formatSeconds(scene.duration_seconds)}</span>
                </td>
                <td data-label="素材の割り当て">
                  <ul className="assembly-timeline-roles">
                    {roleOrder.map((role) => {
                      const status = roleStatusOf(scene, role, missingRoles);
                      const assetId = scene.asset_ids[role];
                      return (
                        <li
                          key={role}
                          className={`assembly-timeline-role assembly-timeline-role--${status}`}
                        >
                          <span className="assembly-timeline-role__glyph" aria-hidden="true">
                            {roleStatusGlyphs[status]}
                          </span>
                          <span className="assembly-timeline-role__name">
                            {sceneRoleLabels[role]}
                          </span>
                          <span className="assembly-timeline-role__status">
                            {assetId
                              ? `${roleStatusLabels[status]}（${assetId}）`
                              : roleStatusLabels[status]}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Read-only pre-export review: scene order, duration, and visual / narration /
 * music assignments, without opening the raw `StoryDocument` JSON.
 *
 * Self-contained on purpose, matching `StoryPanel` and `MatrixPanel`: it lists
 * and selects its own story rather than depending on selection state owned by
 * the Story surface, so it can be mounted anywhere without extra wiring.
 * Missing-asset actions, export controls, and drag/drop editing are explicitly
 * out of scope for this issue (see #62's remaining microtasks).
 */
export function AssemblyTimelinePanel({ projectId }: AssemblyTimelinePanelProps) {
  const [stories, setStories] = useState<StorySummary[]>([]);
  const [listState, setListState] = useState<LoadState>("loading");
  const [selectedStoryId, setSelectedStoryId] = useState("");
  const [detail, setDetail] = useState<StoryDetail | null>(null);
  const [detailState, setDetailState] = useState<LoadState>("ready");
  const [error, setError] = useState("");

  const refreshStories = useCallback(async () => {
    setListState("loading");
    try {
      const response = await listStories({ projectId, limit: 100 });
      setStories(response.items);
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

  return (
    <section
      className="section-card assembly-timeline-panel"
      aria-labelledby="assembly-timeline-panel-heading"
    >
      <div className="section-card__header assembly-timeline-panel__header">
        <div>
          <p className="eyebrow">Assembly</p>
          <h2 id="assembly-timeline-panel-heading">タイムライン</h2>
        </div>
        <p className="section-footnote">
          書き出し前に、シーンの順番・尺・素材の割り当てを確認します。
        </p>
      </div>

      {error ? (
        <div className="error-banner assembly-timeline-error" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="secondary-button"
            onClick={() =>
              void (selectedStoryId ? loadDetail(selectedStoryId) : refreshStories())
            }
          >
            再試行
          </button>
        </div>
      ) : null}

      {listState === "loading" ? (
        <AssemblyTimelineLoading label="ストーリー一覧を読み込んでいます…" />
      ) : (
        <label className="field-group field-group--full">
          <span>表示するストーリー</span>
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
            <h3>表示するストーリーがありません</h3>
            <p>ストーリーを選択すると、シーン順のタイムラインが表示されます。</p>
          </div>
        </div>
      ) : detailState === "loading" ? (
        <AssemblyTimelineLoading label="タイムラインを読み込んでいます…" />
      ) : detail ? (
        <AssemblyTimelineScenes detail={detail} />
      ) : null}
    </section>
  );
}

export default AssemblyTimelinePanel;
