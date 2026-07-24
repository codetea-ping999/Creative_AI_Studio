import { type ModelOption } from "./PromptForm";
import {
  formatPercent,
  formatScore,
  type MediaMetrics,
  type MetricsSummaryResponse,
} from "../studio";

type ModelsSummaryPanelProps = {
  models: ModelOption[];
  metrics: MetricsSummaryResponse | null;
  activeMetrics: MediaMetrics | null;
  mediaLabel: string;
};

export function ModelsSummaryPanel({
  models,
  metrics,
  activeMetrics,
  mediaLabel,
}: ModelsSummaryPanelProps) {
  return (
    <section className="section-card section-card--models">
      <div className="section-card__header">
        <div>
          <p className="eyebrow">Coverage</p>
          <h2>Models and operational summary</h2>
        </div>
      </div>
      <div className="workspace-grid">
        <div className="model-shelf">
          {models.map((model) => (
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
            <p>{mediaLabel} average quality</p>
          </div>
          <div className="metric-pill">
            <strong>{formatPercent(activeMetrics?.feedback_coverage_rate)}</strong>
            <p>{mediaLabel} feedback coverage</p>
          </div>
        </div>
      </div>
    </section>
  );
}

export default ModelsSummaryPanel;
