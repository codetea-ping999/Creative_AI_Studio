import { useCallback, useEffect, useRef, useState } from "react";
import {
  countSceneAssetStates,
  generateSceneMedia,
  getStory,
  listStories,
  sceneAssetStatusLookup,
  sceneRoleLabels,
  type SceneAssetState,
  type SceneAssetStatusEntry,
  type SceneRole,
  type StoryDetail,
  type StoryScene,
  type StorySummary,
} from "../lib/storyApi";
import { terminalStatuses, type JobResponse, type JobStatus } from "../studio";
import { requestJson } from "../studioClient";

export type AssemblyTimelinePanelProps = {
  projectId?: string;
};

type LoadState = "loading" | "ready" | "error";

/** Roles are always shown in production order: what you see, then what you hear. */
const roleOrder: SceneRole[] = ["visual", "narration", "music"];

/**
 * Whether one scene/role pair is filled, still needed, actively generating,
 * failed, or genuinely optional (issue #245 adds the middle two: without
 * them a role mid-generation or one whose only attempt failed looked
 * identical to one nobody has touched yet).
 */
type RoleStatus = SceneAssetState;

const roleStatusLabels: Record<RoleStatus, string> = {
  assigned: "割り当て済み",
  missing: "未割り当て",
  optional: "未設定（任意）",
  generating: "生成中",
  failed: "生成失敗",
};

// A glyph carries the state on its own so the badge still reads correctly for
// color-blind users or in a high-contrast/no-color theme — text alone would
// also work, but the glyph gives a scannable column when scenes stack up.
const roleStatusGlyphs: Record<RoleStatus, string> = {
  assigned: "✓",
  missing: "!",
  optional: "–",
  generating: "…",
  failed: "✕",
};

function formatSeconds(value: number): string {
  return `${value.toFixed(1)} 秒`;
}

/**
 * Key one scene/role's in-flight generation.
 *
 * Scoped by story id, not just scene id: scene ids are per-story sequential
 * (`scene_01`, `scene_02`, …, see `core/story/merge.py`), so two different
 * stories can share the same scene id. Without the story id, a generation
 * launched from one story could incorrectly disable an unrelated scene's
 * button in another.
 */
function busyKey(storyId: string, sceneId: string, role: SceneRole): string {
  return `${storyId}:${sceneId}:${role}`;
}

/**
 * Poll a job until it reaches a terminal status and return that status.
 *
 * Mirrors `App.tsx`'s `awaitJobCompletion`, duplicated rather than shared: this
 * panel is deliberately self-contained (see `AssemblyTimelinePanel`'s doc
 * comment) so it can be mounted without a parent wiring a job-polling prop
 * through, matching how `LatestJobPanel` reads `terminalStatuses`/`JobResponse`
 * directly instead of taking them as props.
 */
async function pollJobStatus(jobId: string): Promise<JobStatus> {
  const intervalMs = 1200;
  const deadline = Date.now() + 10 * 60 * 1000;
  for (;;) {
    const payload = await requestJson<JobResponse>(`/jobs/${jobId}`);
    if (terminalStatuses.has(payload.status) || Date.now() > deadline) {
      return payload.status;
    }
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
  }
}

/**
 * Resolve one scene/role's status.
 *
 * `statusEntry` (from `detail.asset_status`) is preferred when present since
 * it also knows about in-flight and failed generation jobs; a `detail` that
 * predates issue #245 (or a fixture that only sets `missing_assets`) falls
 * back to the original assigned/missing/optional derivation.
 */
function roleStatusOf(
  scene: StoryScene,
  role: SceneRole,
  missingRoles: Set<SceneRole>,
  statusEntry: SceneAssetStatusEntry | undefined,
): RoleStatus {
  if (statusEntry) {
    return statusEntry.state;
  }
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
 * Read-only for layout and export (issue #244: no reordering, no export
 * controls — later, separate microtasks of #62). Issue #246 adds one
 * exception: a missing or failed role gets a generate/retry action right on
 * its row, so a gap found here doesn't require switching to the Story
 * surface and rebuilding its context by hand.
 */
function AssemblyTimelineScenes({
  detail,
  busyRoles,
  onGenerateRole,
}: {
  detail: StoryDetail;
  busyRoles: Set<string>;
  onGenerateRole: (scene: StoryScene, role: SceneRole) => void;
}) {
  const scenes = [...detail.story.scenes].sort((left, right) => left.order - right.order);
  const totalDuration = scenes.reduce((total, scene) => total + scene.duration_seconds, 0);

  const missingByScene = new Map<string, Set<SceneRole>>();
  for (const entry of detail.missing_assets) {
    const roles = missingByScene.get(entry.scene_id) ?? new Set<SceneRole>();
    roles.add(entry.role as SceneRole);
    missingByScene.set(entry.scene_id, roles);
  }
  const statusByScene = sceneAssetStatusLookup(detail);

  // `detail.asset_status` is the single source both the per-scene rows and
  // this summary read from, so the two can never disagree (issue #245).
  // A `detail` with no `asset_status` at all (predates #245) falls back to
  // `missing_assets` alone, matching this panel's original #244 summary.
  const hasAssetStatus = (detail.asset_status?.length ?? 0) > 0;
  const stateCounts = countSceneAssetStates(detail);
  const missingCount = hasAssetStatus ? stateCounts.missing : detail.missing_assets.length;
  const summaryParts: string[] = [];
  if (missingCount > 0) {
    summaryParts.push(`不足素材 ${missingCount} 件`);
  }
  if (stateCounts.generating > 0) {
    summaryParts.push(`生成中 ${stateCounts.generating} 件`);
  }
  if (stateCounts.failed > 0) {
    summaryParts.push(`失敗 ${stateCounts.failed} 件`);
  }
  const summarySuffix = summaryParts.length > 0
    ? ` — ${summaryParts.join(" / ")}`
    : " — 素材はすべて揃っています";

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
          {summarySuffix}
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
            const sceneStatus = statusByScene.get(scene.id);
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
                      const statusEntry = sceneStatus?.get(role);
                      const status = roleStatusOf(scene, role, missingRoles, statusEntry);
                      const assetId = scene.asset_ids[role];
                      const isBusy =
                        status === "generating" ||
                        busyRoles.has(busyKey(detail.story.id, scene.id, role));
                      const canLaunch = status === "missing" || status === "failed";
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
                          {status === "failed" && statusEntry?.error_message ? (
                            <details className="assembly-timeline-role__reason">
                              <summary>失敗の理由</summary>
                              <p>{statusEntry.error_message}</p>
                            </details>
                          ) : null}
                          {isBusy ? (
                            <button
                              type="button"
                              className="secondary-button assembly-timeline-role__action"
                              disabled
                              aria-busy="true"
                            >
                              {sceneRoleLabels[role]}を生成中…
                            </button>
                          ) : canLaunch ? (
                            <button
                              type="button"
                              className="secondary-button assembly-timeline-role__action"
                              onClick={() => onGenerateRole(scene, role)}
                            >
                              {status === "failed"
                                ? `${sceneRoleLabels[role]}を再試行`
                                : `${sceneRoleLabels[role]}を生成`}
                            </button>
                          ) : null}
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
 * Pre-export review: scene order, duration, and visual / narration / music
 * assignments, without opening the raw `StoryDocument` JSON.
 *
 * Self-contained on purpose, matching `StoryPanel` and `MatrixPanel`: it lists
 * and selects its own story rather than depending on selection state owned by
 * the Story surface, so it can be mounted anywhere without extra wiring —
 * including polling its own launched jobs (see `pollJobStatus`) rather than
 * taking an `awaitJob` prop the way `StoryPanel` does. Export controls and
 * drag/drop editing stay out of scope (see #62's remaining microtasks); issue
 * #246 adds the one exception noted on `AssemblyTimelineScenes`: launching
 * generation for a missing or failed role straight from its scene row.
 */
export function AssemblyTimelinePanel({ projectId }: AssemblyTimelinePanelProps) {
  const [stories, setStories] = useState<StorySummary[]>([]);
  const [listState, setListState] = useState<LoadState>("loading");
  const [selectedStoryId, setSelectedStoryId] = useState("");
  const [detail, setDetail] = useState<StoryDetail | null>(null);
  const [detailState, setDetailState] = useState<LoadState>("ready");
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busyRoles, setBusyRoles] = useState<Set<string>>(new Set());

  // Read inside async generation handlers to tell whether the user has since
  // switched to a different story, so a job launched from story A can't clobber
  // story B's freshly-loaded detail with a late-arriving refresh.
  const selectedStoryIdRef = useRef(selectedStoryId);
  useEffect(() => {
    selectedStoryIdRef.current = selectedStoryId;
  }, [selectedStoryId]);

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
    setActionError("");
    void loadDetail(selectedStoryId);
  }, [loadDetail, selectedStoryId]);

  /**
   * Launch (or retry) one scene/role's generation from its timeline row.
   *
   * The request is built server-side from the scene itself — `generateSceneMedia`
   * only ever carries `storyId`/`sceneId`/`role` — so it can't drift onto whatever
   * the unrelated Composer surface currently holds (issue #246's "build requests
   * from the Story/timeline scene context" criterion).
   */
  const handleGenerateRole = useCallback(
    async (scene: StoryScene, role: SceneRole) => {
      if (!detail) return;
      const storyId = detail.story.id;
      const key = busyKey(storyId, scene.id, role);
      if (busyRoles.has(key)) {
        // Already in flight for this exact scene/role — the button renders
        // disabled once this state lands, but the guard also covers the gap
        // between a click and that re-render.
        return;
      }
      setBusyRoles((previous) => new Set(previous).add(key));
      setActionError("");
      try {
        const { job_id: jobId } = await generateSceneMedia(storyId, scene.id, { role });
        // Refresh right away so the row can show "generating" instead of
        // sitting on "missing"/"failed" until the job finishes.
        if (selectedStoryIdRef.current === storyId) {
          setDetail(await getStory(storyId));
        }
        const finalStatus = await pollJobStatus(jobId);
        if (selectedStoryIdRef.current === storyId) {
          setDetail(await getStory(storyId));
        }
        if (finalStatus !== "succeeded") {
          setActionError(
            `${sceneRoleLabels[role]}の生成が ${finalStatus} で終了しました（${scene.heading || scene.id}）。`,
          );
        }
      } catch (cause) {
        setActionError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusyRoles((previous) => {
          const next = new Set(previous);
          next.delete(key);
          return next;
        });
      }
    },
    [detail, busyRoles],
  );

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

      {actionError ? (
        <div className="error-banner assembly-timeline-error" role="alert">
          <span>{actionError}</span>
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
        <AssemblyTimelineScenes
          detail={detail}
          busyRoles={busyRoles}
          onGenerateRole={(scene, role) => void handleGenerateRole(scene, role)}
        />
      ) : null}
    </section>
  );
}

export default AssemblyTimelinePanel;
