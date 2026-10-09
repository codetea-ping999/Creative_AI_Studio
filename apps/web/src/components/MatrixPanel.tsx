import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  advanceBatch,
  batchProgressLabel,
  cancelBatch,
  createBatchFromTemplate,
  currentStageItems,
  getBatch,
  listBatches,
  listBatchTemplates,
  promoteBatchItem,
  terminalBatchStatuses,
  type Batch,
  type BatchItem,
  type BatchTemplate,
} from "../lib/batchApi";
import { createOutputUrl } from "../studioClient";

export type MatrixPanelProps = {
  /** Model used for the sweep; empty falls back to the API default. */
  modelId: string;
  projectId?: string;
  /** Milliseconds between progress refreshes while a batch is running. */
  pollIntervalMs?: number;
  onInspectItem?: (item: BatchItem) => void;
};

type LoadState = "loading" | "ready" | "error";
type ItemFilter = "all" | "succeeded" | "failed" | "promoted" | "rated";
type QuickRating = "" | "keep" | "maybe" | "reject";

const statusLabels: Record<string, string> = {
  pending: "待機",
  queued: "待機",
  preparing: "準備中",
  running: "生成中",
  postprocessing: "仕上げ中",
  succeeded: "完了",
  partial: "一部完了",
  failed: "失敗",
  cancelled: "中断",
};

const ratingLabels: Record<QuickRating, string> = {
  "": "未評価",
  keep: "候補",
  maybe: "保留",
  reject: "除外",
};

function statusLabel(status: string): string {
  return statusLabels[status] ?? status;
}

function MatrixLoadingGrid() {
  return (
    <div className="matrix-loading-grid" role="status" aria-busy="true">
      <span className="matrix-loading-grid__label">比較結果を待っています…</span>
      {Array.from({ length: 6 }, (_, index) => (
        <span key={index} className="matrix-loading-cell" aria-hidden="true" />
      ))}
    </div>
  );
}

function MatrixInspector({
  item,
  onClose,
}: {
  item: BatchItem;
  onClose: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const previewUrl = createOutputUrl(item.preview_path ?? item.output_path);

  useEffect(() => {
    headingRef.current?.focus();
  }, [item.id]);

  return (
    <section className="matrix-inspector" aria-labelledby="matrix-inspector-heading">
      <div className="matrix-inspector__header">
        <div>
          <p className="eyebrow">Preview</p>
          <h3 id="matrix-inspector-heading" ref={headingRef} tabIndex={-1}>
            {item.label}
          </h3>
        </div>
        <button type="button" className="secondary-button" onClick={onClose}>
          閉じる
        </button>
      </div>
      <div className="matrix-inspector__content">
        <div className="matrix-inspector__media">
          {previewUrl ? (
            <img src={previewUrl} alt={`${item.label} の拡大プレビュー`} />
          ) : (
            <span>プレビューはありません</span>
          )}
        </div>
        <dl className="matrix-axis-detail">
          <div>
            <dt>スコア</dt>
            <dd>{item.score !== null ? item.score.toFixed(1) : "未採点"}</dd>
          </div>
          {Object.entries(item.axis_values).map(([axis, value]) => (
            <div key={axis}>
              <dt>{axis}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

function MatrixCell({
  item,
  isBest,
  rating,
  isPromoting,
  onRatingChange,
  onPromote,
  onPreview,
  onInspectItem,
}: {
  item: BatchItem;
  isBest: boolean;
  rating: QuickRating;
  isPromoting: boolean;
  onRatingChange: (rating: QuickRating) => void;
  onPromote: () => void;
  onPreview: () => void;
  onInspectItem?: (item: BatchItem) => void;
}) {
  const previewUrl = createOutputUrl(item.preview_path ?? item.output_path);
  const canInspect = item.status === "succeeded";

  return (
    <li
      className={`matrix-cell ${item.promoted ? "is-promoted" : ""}`}
      aria-label={`${item.label}、${statusLabel(item.status)}${
        item.promoted ? "、採用済み" : ""
      }`}
    >
      <button
        type="button"
        className="matrix-cell__thumb"
        onClick={onPreview}
        disabled={!previewUrl || !canInspect}
        aria-label={`${item.label}を拡大表示`}
      >
        {previewUrl && canInspect ? (
          <img
            src={previewUrl}
            alt={item.label}
            loading="lazy"
            decoding="async"
          />
        ) : (
          <span className="matrix-cell__placeholder">
            {item.status === "failed"
              ? "生成に失敗"
              : item.status === "cancelled"
                ? "中断済み"
                : `${statusLabel(item.status)}…`}
          </span>
        )}
      </button>
      <div className="matrix-cell__body">
        <div className="matrix-cell__heading">
          <strong className="matrix-cell__label">{item.label}</strong>
          <span className="history-score" aria-label="ヒューリスティックスコア">
            {item.score !== null ? item.score.toFixed(1) : "未採点"}
          </span>
        </div>
        <div className="matrix-cell__meta">
          <span className={`status-chip status-chip--${item.status}`}>
            {statusLabel(item.status)}
          </span>
          {isBest ? <span className="matrix-tag">最高スコア</span> : null}
          {item.promoted ? <span className="matrix-tag">採用済み</span> : null}
        </div>
        <dl className="matrix-axis-values" aria-label="生成軸">
          {Object.entries(item.axis_values).map(([axis, value]) => (
            <div key={axis}>
              <dt>{axis}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        {item.error_message ? (
          <p className="matrix-cell__error" role="alert">
            {item.error_message}
          </p>
        ) : null}
        <label className="matrix-rating">
          <span>クイック評価</span>
          <select
            value={rating}
            onChange={(event) => onRatingChange(event.target.value as QuickRating)}
            aria-label={`${item.label}のクイック評価`}
          >
            {Object.entries(ratingLabels).map(([value, label]) => (
              <option key={value || "unrated"} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <div className="matrix-cell__actions">
          <button
            type="button"
            className="secondary-button"
            onClick={onPromote}
            disabled={!canInspect || item.promoted || isPromoting}
            aria-busy={isPromoting}
          >
            {isPromoting ? "採用中…" : item.promoted ? "採用済み" : "採用"}
          </button>
          {onInspectItem ? (
            <button
              type="button"
              className="secondary-button"
              onClick={() => onInspectItem(item)}
              disabled={!canInspect}
            >
              ジョブ詳細
            </button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export function MatrixPanel({
  modelId,
  projectId,
  pollIntervalMs = 2500,
  onInspectItem,
}: MatrixPanelProps) {
  const [templates, setTemplates] = useState<BatchTemplate[]>([]);
  const [recentBatches, setRecentBatches] = useState<Batch[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [templateName, setTemplateName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [batch, setBatch] = useState<Batch | null>(null);
  const [filter, setFilter] = useState<ItemFilter>("all");
  const [ratings, setRatings] = useState<Record<string, QuickRating>>({});
  const [previewItem, setPreviewItem] = useState<BatchItem | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isAdvancing, setIsAdvancing] = useState(false);
  const [promotingIds, setPromotingIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const loadSources = useCallback(async () => {
    setLoadState("loading");
    setError("");
    const [templateResult, batchResult] = await Promise.allSettled([
      listBatchTemplates(),
      listBatches({ projectId, limit: 12 }),
    ]);
    if (templateResult.status === "rejected") {
      setTemplates([]);
      setLoadState("error");
      setError(
        templateResult.reason instanceof Error
          ? templateResult.reason.message
          : String(templateResult.reason),
      );
      return;
    }
    setTemplates(templateResult.value);
    setTemplateName((current) =>
      templateResult.value.some((entry) => entry.name === current)
        ? current
        : templateResult.value.find((entry) => !entry.error)?.name ?? "",
    );
    setRecentBatches(
      batchResult.status === "fulfilled" ? batchResult.value.items : [],
    );
    setLoadState("ready");
  }, [projectId]);

  useEffect(() => {
    setBatch(null);
    setPreviewItem(null);
    setRatings({});
    void loadSources();
  }, [loadSources]);

  const refresh = useCallback(async (batchId: string) => {
    try {
      const next = await getBatch(batchId);
      setBatch(next);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  // A timeout schedules the next read only after the previous response is applied,
  // preventing overlapping requests on slower local inference machines.
  useEffect(() => {
    if (!batch || terminalBatchStatuses.has(batch.status)) return;
    const timer = window.setTimeout(() => {
      void refresh(batch.id);
    }, pollIntervalMs);
    return () => window.clearTimeout(timer);
  }, [batch, pollIntervalMs, refresh]);

  const selectedTemplate = useMemo(
    () => templates.find((entry) => entry.name === templateName) ?? null,
    [templates, templateName],
  );
  const items = useMemo(() => currentStageItems(batch), [batch]);
  const filteredItems = useMemo(
    () =>
      items.filter((item) => {
        if (filter === "all") return true;
        if (filter === "promoted") return item.promoted;
        if (filter === "rated") return Boolean(ratings[item.id]);
        return item.status === filter;
      }),
    [filter, items, ratings],
  );
  const plannedItems = selectedTemplate?.first_stage_items ?? 0;
  const doneCount = batch
    ? batch.aggregate.succeeded +
      batch.aggregate.failed +
      batch.aggregate.cancelled
    : 0;
  const canAdvance =
    Boolean(batch) &&
    terminalBatchStatuses.has(batch?.status ?? "") &&
    (batch?.stage_index ?? 0) < (batch?.stage_names.length ?? 0) - 1;

  async function handleSubmit() {
    if (!templateName || !prompt.trim() || selectedTemplate?.error) return;
    setError("");
    setNotice("");
    setIsSubmitting(true);
    try {
      const created = await createBatchFromTemplate({
        template: templateName,
        overrides: {
          prompt: prompt.trim(),
          model_id: modelId,
          ...(projectId ? { project_id: projectId } : {}),
        },
      });
      setBatch(created);
      setRatings({});
      setFilter("all");
      setPreviewItem(null);
      setNotice(
        `${created.aggregate.total} 件の生成を開始しました。完了した項目から比較できます。`,
      );
      setRecentBatches((current) => [
        created,
        ...current.filter((entry) => entry.id !== created.id),
      ]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleCancel() {
    if (!batch || isCancelling) return;
    setIsCancelling(true);
    setError("");
    try {
      const next = await cancelBatch(batch.id);
      setBatch(next);
      setNotice("未完了の生成を中断しました。完了済みの比較結果は残ります。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setIsCancelling(false);
    }
  }

  async function handleAdvance() {
    if (!batch || !canAdvance || isAdvancing) return;
    setIsAdvancing(true);
    setError("");
    try {
      const next = await advanceBatch(batch.id);
      setBatch(next);
      setFilter("all");
      setPreviewItem(null);
      setNotice(
        `${next.stage_names[next.stage_index] ?? "次の段階"}を開始しました。`,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setIsAdvancing(false);
    }
  }

  async function handlePromote(item: BatchItem) {
    if (!batch || promotingIds.has(item.id)) return;
    setPromotingIds((current) => new Set(current).add(item.id));
    setError("");
    try {
      const next = await promoteBatchItem(batch.id, item.id);
      setBatch(next);
      setNotice(`「${item.label}」を採用候補にしました。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPromotingIds((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  }

  return (
    <section className="section-card matrix-panel" aria-labelledby="matrix-panel-heading">
      <div className="section-card__header matrix-panel__header">
        <div>
          <p className="eyebrow">Matrix</p>
          <h2 id="matrix-panel-heading">多重生成して比較する</h2>
        </div>
        <p className="section-footnote">
          固定軸を一括生成し、スコアと目視評価から仕上げ候補を選びます。
        </p>
      </div>

      {error ? (
        <div className="error-banner matrix-error" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="secondary-button"
            onClick={() =>
              void (batch ? refresh(batch.id) : loadSources())
            }
          >
            再試行
          </button>
        </div>
      ) : null}
      <p className="matrix-notice" role="status" aria-live="polite">
        {notice}
      </p>

      {loadState === "loading" ? (
        <div className="surface-loading" role="status" aria-busy="true">
          <span className="surface-loading__bar" aria-hidden="true" />
          <span>比較プリセットを読み込んでいます…</span>
        </div>
      ) : loadState === "ready" && templates.length === 0 ? (
        <div className="empty-stage matrix-empty-stage">
          <div>
            <h3>利用できるプリセットがありません</h3>
            <p>パターンカタログを確認してから再読み込みしてください。</p>
          </div>
        </div>
      ) : (
        <div className="matrix-composer">
          <div className="field-grid">
            <label className="field-group">
              <span>比較プリセット</span>
              <select
                value={templateName}
                onChange={(event) => setTemplateName(event.target.value)}
                disabled={isSubmitting}
              >
                {templates.map((entry) => (
                  <option key={entry.name} value={entry.name} disabled={Boolean(entry.error)}>
                    {entry.name}{entry.error ? "（利用不可）" : ""}
                  </option>
                ))}
              </select>
            </label>
            <label className="field-group field-group--full">
              <span>お題</span>
              <input
                type="text"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder="例: acme coffee roasters のロゴ"
                disabled={isSubmitting}
                aria-describedby="matrix-plan-summary"
              />
            </label>
          </div>

          {selectedTemplate ? (
            <div
              id="matrix-plan-summary"
              className={`matrix-plan ${selectedTemplate.error ? "is-error" : ""}`}
            >
              <div>
                <span>生成予定</span>
                <strong>{plannedItems || "—"} 件</strong>
              </div>
              <p>{selectedTemplate.description}</p>
              {selectedTemplate.axes?.length ? (
                <dl className="matrix-template-axes" aria-label="プリセットの固定軸">
                  {selectedTemplate.axes.map((axis) => (
                    <div key={axis.name}>
                      <dt>{axis.name}</dt>
                      <dd>{axis.value_count} 値</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {selectedTemplate.stages && selectedTemplate.stages.length > 1 ? (
                <p>
                  1 段階目は {plannedItems} 件。上位
                  {selectedTemplate.stages[0].keep_top_n ?? "指定"}件だけを
                  {selectedTemplate.stages[1].name} へ進めます。
                </p>
              ) : null}
              {selectedTemplate.error ? (
                <p role="alert">利用不可: {selectedTemplate.error}</p>
              ) : null}
            </div>
          ) : null}

          <div className="form-actions matrix-composer__actions">
            <button
              type="button"
              onClick={() => void handleSubmit()}
              disabled={
                isSubmitting ||
                !templateName ||
                !prompt.trim() ||
                Boolean(selectedTemplate?.error)
              }
              aria-busy={isSubmitting}
              aria-describedby="matrix-plan-summary"
            >
              {isSubmitting
                ? "起動中…"
                : plannedItems > 0
                  ? `${plannedItems} 件を生成`
                  : "生成"}
            </button>
            {!prompt.trim() ? <span>比較したいお題を入力してください。</span> : null}
          </div>
        </div>
      )}

      {recentBatches.length > 0 ? (
        <label className="field-group matrix-history-select">
          <span>保存済みの比較を再開</span>
          <select
            value={batch?.id ?? ""}
            onChange={(event) => {
              const nextId = event.target.value;
              const cached =
                recentBatches.find((entry) => entry.id === nextId) ?? null;
              setBatch(cached);
              setPreviewItem(null);
              setFilter("all");
              if (nextId) void refresh(nextId);
            }}
          >
            <option value="">新しい比較を開始</option>
            {recentBatches.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name} — {statusLabel(entry.status)} —{" "}
                {new Date(entry.updated_at).toLocaleString("ja-JP")}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      {!batch ? (
        loadState === "ready" && templates.length > 0 ? (
          <div className="empty-stage matrix-empty-stage">
            <div>
              <h3>比較結果はまだありません</h3>
              <p>
                プリセットとお題を決めて実行すると、比較グリッドが表示されます。完了した結果から最大30件を一覧比較できます。
              </p>
            </div>
          </div>
        ) : null
      ) : (
        <div className="matrix-results">
          <div className="matrix-progress" role="status" aria-live="polite">
            <div className="matrix-progress__copy">
              <span className={`status-chip status-chip--${batch.status}`}>
                {statusLabel(batch.status)}
              </span>
              <strong>{batchProgressLabel(batch)}</strong>
              <span>
                Stage {batch.stage_index + 1} / {batch.stage_names.length}
              </span>
            </div>
            {batch.status === "failed" && batch.error_message ? (
              // Distinct from a batch failed via failed items (already
              // explained by 失敗 in the metrics below): this is a
              // stage-advance preflight rejection, so aggregate.failed can
              // legitimately be 0 while the batch still shows as failed.
              <div className="error-banner matrix-progress__error" role="alert">
                <span>{batch.error_message}</span>
              </div>
            ) : null}
            <progress
              value={doneCount}
              max={Math.max(batch.aggregate.total, 1)}
              aria-label={`バッチ進捗 ${doneCount}/${batch.aggregate.total}`}
            />
            <dl className="matrix-progress__metrics">
              <div>
                <dt>成功</dt>
                <dd>{batch.aggregate.succeeded}</dd>
              </div>
              <div>
                <dt>失敗</dt>
                <dd>{batch.aggregate.failed}</dd>
              </div>
              <div>
                <dt>平均</dt>
                <dd>
                  {batch.aggregate.average_score !== null
                    ? batch.aggregate.average_score.toFixed(1)
                    : "—"}
                </dd>
              </div>
            </dl>
            <div className="matrix-progress__actions">
              {canAdvance ? (
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void handleAdvance()}
                  disabled={isAdvancing}
                  aria-busy={isAdvancing}
                >
                  {isAdvancing ? "仕上げ開始中…" : "上位候補を仕上げる"}
                </button>
              ) : null}
              {!terminalBatchStatuses.has(batch.status) ? (
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void handleCancel()}
                  disabled={isCancelling}
                  aria-busy={isCancelling}
                >
                  {isCancelling ? "中断中…" : "中断する"}
                </button>
              ) : null}
            </div>
          </div>

          {previewItem ? (
            <MatrixInspector item={previewItem} onClose={() => setPreviewItem(null)} />
          ) : null}

          {items.length > 0 ? (
            <div className="matrix-toolbar">
              <label className="field-group">
                <span>表示する結果</span>
                <select
                  value={filter}
                  onChange={(event) => setFilter(event.target.value as ItemFilter)}
                >
                  <option value="all">すべて（{items.length}）</option>
                  <option value="succeeded">完了</option>
                  <option value="failed">失敗</option>
                  <option value="promoted">採用済み</option>
                  <option value="rated">評価済み</option>
                </select>
              </label>
              <p>
                {filteredItems.length} / {items.length} 件を表示
                <span>クイック評価はこの画面を開いている間だけ保持されます。</span>
              </p>
            </div>
          ) : null}

          {items.length === 0 && !terminalBatchStatuses.has(batch.status) ? (
            <MatrixLoadingGrid />
          ) : items.length === 0 ? (
            <div className="empty-stage matrix-empty-stage">
              <div>
                <h3>この段階の結果はありません</h3>
                <p>すべて失敗または中断された場合は、お題やモデルを見直してください。</p>
              </div>
            </div>
          ) : filteredItems.length === 0 ? (
            <div className="empty-stage matrix-empty-stage">
              <div>
                <h3>条件に合う結果がありません</h3>
                <p>別の表示条件を選ぶと、生成済みの結果を確認できます。</p>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => setFilter("all")}
                >
                  すべて表示
                </button>
              </div>
            </div>
          ) : (
            <ul className="matrix-grid" aria-label="生成結果の比較">
              {filteredItems.map((item) => (
                <MatrixCell
                  key={item.id}
                  item={item}
                  isBest={batch.aggregate.best_item_id === item.id}
                  rating={ratings[item.id] ?? ""}
                  isPromoting={promotingIds.has(item.id)}
                  onRatingChange={(rating) =>
                    setRatings((current) => ({ ...current, [item.id]: rating }))
                  }
                  onPromote={() => void handlePromote(item)}
                  onPreview={() => setPreviewItem(item)}
                  onInspectItem={onInspectItem}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
