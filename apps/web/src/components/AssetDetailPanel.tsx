import { useState, type FormEvent } from "react";
import { StagePreview } from "./MediaPreview";
import {
  getQuickReviewIssueOptions,
  type QuickReviewIssueTag,
} from "../lib/quickReview";
import {
  formatDate,
  formatScore,
  type GalleryAssetDetailResponse,
  type ProjectResponse,
} from "../studio";

export type FeedbackFormValues = {
  qualityRating: number;
  semanticRating: number | null;
  creativeRating: number | null;
  reuseIntent: boolean;
  exportReady: boolean;
  issueTags: string[];
  comments: string;
};

type AssetDetailPanelProps = {
  detail: GalleryAssetDetailResponse;
  projects: ProjectResponse[];
  assetProjectId: string;
  onAssetProjectIdChange: (value: string) => void;
  isAssetBusy: boolean;
  isFeedbackBusy: boolean;
  onOpenQuickReview: () => void;
  onQuickReview: (
    kind: "accept" | "revise" | "rerun",
    issueTags: QuickReviewIssueTag[],
  ) => Promise<boolean>;
  onReuse: () => void;
  canConditionMelody: boolean;
  melodyConditioningMessage: string;
  onConditionMelody: () => void;
  onLoadIntoComposer: () => void;
  onExport: () => void;
  onBindProject: () => void;
  onSubmitFeedback: (values: FeedbackFormValues) => Promise<boolean>;
};

const RATING_OPTIONS = [1, 2, 3, 4, 5];

/**
 * Detail view for the selected gallery asset. Owns the feedback and
 * quick-review form state locally; mount with `key={detail.asset_id}` so those
 * transient fields reset when a different asset is selected.
 */
export function AssetDetailPanel({
  detail,
  projects,
  assetProjectId,
  onAssetProjectIdChange,
  isAssetBusy,
  isFeedbackBusy,
  onOpenQuickReview,
  onQuickReview,
  onReuse,
  canConditionMelody,
  melodyConditioningMessage,
  onConditionMelody,
  onLoadIntoComposer,
  onExport,
  onBindProject,
  onSubmitFeedback,
}: AssetDetailPanelProps) {
  const [isQuickReviewOpen, setIsQuickReviewOpen] = useState(false);
  const [quickReviewIssueTags, setQuickReviewIssueTags] = useState<QuickReviewIssueTag[]>([]);
  const [feedbackQuality, setFeedbackQuality] = useState("4");
  const [feedbackSemantic, setFeedbackSemantic] = useState("4");
  const [feedbackCreative, setFeedbackCreative] = useState("4");
  const [feedbackReuseIntent, setFeedbackReuseIntent] = useState(false);
  const [feedbackExportReady, setFeedbackExportReady] = useState(false);
  const [feedbackIssueTags, setFeedbackIssueTags] = useState("");
  const [feedbackComments, setFeedbackComments] = useState("");

  function closeQuickReview(): void {
    setIsQuickReviewOpen(false);
    setQuickReviewIssueTags([]);
  }

  function openQuickReview(): void {
    onOpenQuickReview();
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

  async function runQuickReview(
    kind: "accept" | "revise" | "rerun",
    issueTags: QuickReviewIssueTag[],
  ): Promise<void> {
    const shouldClose = await onQuickReview(kind, issueTags);
    if (shouldClose) {
      closeQuickReview();
    }
  }

  async function handleFeedbackSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const semanticRating = Number.parseInt(feedbackSemantic, 10);
    const creativeRating = Number.parseInt(feedbackCreative, 10);
    const saved = await onSubmitFeedback({
      qualityRating: Number.parseInt(feedbackQuality, 10),
      semanticRating: Number.isFinite(semanticRating) ? semanticRating : null,
      creativeRating: Number.isFinite(creativeRating) ? creativeRating : null,
      reuseIntent: feedbackReuseIntent,
      exportReady: feedbackExportReady,
      issueTags: feedbackIssueTags
        .split(",")
        .map((tag) => tag.trim())
        .filter(Boolean),
      comments: feedbackComments.trim(),
    });
    if (saved) {
      setFeedbackComments("");
      setFeedbackIssueTags("");
    }
  }

  return (
    <div className="detail-grid">
      <div className="stage-stack">
        <StagePreview
          mediaType={detail.media_type}
          outputPath={detail.preview_path}
          title={detail.prompt}
          subtitle={detail.project_name || "Unassigned"}
        />
        <div className="asset-actions">
          <button
            type="button"
            className="dock-submit"
            onClick={() => {
              void runQuickReview("accept", []);
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
              void runQuickReview("rerun", []);
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
              {getQuickReviewIssueOptions(detail.media_type).map((option) => {
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
                onClick={closeQuickReview}
                disabled={isFeedbackBusy || isAssetBusy}
              >
                キャンセル
              </button>
              <button
                type="button"
                className="dock-submit"
                onClick={() => {
                  void runQuickReview("revise", quickReviewIssueTags);
                }}
                disabled={isFeedbackBusy || isAssetBusy || quickReviewIssueTags.length === 0}
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
            onClick={onReuse}
            disabled={isAssetBusy}
          >
            Reuse and rerun
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={onLoadIntoComposer}
            disabled={isAssetBusy}
          >
            Load into composer
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={onExport}
            disabled={isAssetBusy}
          >
            Export asset
          </button>
        </div>
        {detail.media_type === "audio" ? (
          <div className="melody-conditioning-action">
            <button
              type="button"
              className="secondary-button"
              onClick={onConditionMelody}
              disabled={isAssetBusy || !canConditionMelody}
              aria-describedby="melody-conditioning-help"
            >
              Use as melody reference
            </button>
            <p id="melody-conditioning-help" className="section-footnote">
              {melodyConditioningMessage}
            </p>
          </div>
        ) : null}
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
              value={assetProjectId}
              onChange={(event) => onAssetProjectIdChange(event.target.value)}
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
            onClick={onBindProject}
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
            <strong>{detail.asset_id}</strong>
          </div>
          <div className="metadata-item">
            <span>Model</span>
            <strong>{detail.model_id}</strong>
          </div>
          <div className="metadata-item">
            <span>Created</span>
            <strong>{formatDate(detail.created_at)}</strong>
          </div>
          <div className="metadata-item">
            <span>Updated</span>
            <strong>{formatDate(detail.updated_at)}</strong>
          </div>
          <div className="metadata-item">
            <span>Quality</span>
            <strong>
              {formatScore(detail.quality_score_calibrated ?? detail.quality_score)}
            </strong>
          </div>
          <div className="metadata-item">
            <span>Semantic</span>
            <strong>
              {formatScore(
                detail.semantic_alignment_score_calibrated ?? detail.semantic_alignment_score,
              )}
            </strong>
          </div>
          <div className="metadata-item">
            <span>Creative</span>
            <strong>
              {formatScore(
                detail.creative_alignment_score_calibrated ?? detail.creative_alignment_score,
              )}
            </strong>
          </div>
          <div className="metadata-item">
            <span>Feedback</span>
            <strong>{detail.feedback_count}</strong>
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
            <span className="form-section__mode">reuse {detail.reuse_count}</span>
            <span className="form-section__mode">export {detail.export_count}</span>
            <span className="form-section__mode">feedback {detail.feedback_count}</span>
          </div>
          <div className="asset-list">
            <div className="asset-path">
              <span>Parent Asset</span>
              <code>{detail.parent_asset_id ?? "none"}</code>
            </div>
            <div className="asset-path">
              <span>Lineage</span>
              <code>{detail.lineage.length > 0 ? detail.lineage.join(", ") : "none"}</code>
            </div>
            <div className="asset-path">
              <span>Exports</span>
              <code>
                {detail.export_paths.length > 0 ? detail.export_paths.join(", ") : "none"}
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
                {RATING_OPTIONS.map((rating) => (
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
                {RATING_OPTIONS.map((rating) => (
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
                {RATING_OPTIONS.map((rating) => (
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

        <pre>{JSON.stringify(detail.request_snapshot, null, 2)}</pre>
      </div>
    </div>
  );
}

export default AssetDetailPanel;
