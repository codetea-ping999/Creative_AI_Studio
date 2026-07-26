import { StagePreview } from "./MediaPreview";
import {
  extractJobQualityScore,
  formatDate,
  formatPercent,
  formatScore,
  terminalStatuses,
  type JobResponse,
} from "../studio";

type LatestJobPanelProps = {
  latestJob: JobResponse | null;
  onCancel: () => void;
};

export function LatestJobPanel({ latestJob, onCancel }: LatestJobPanelProps) {
  return (
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
            <button type="button" className="secondary-button" onClick={onCancel}>
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
  );
}

export default LatestJobPanel;
