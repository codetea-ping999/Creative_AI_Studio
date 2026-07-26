import { useRef, useState } from "react";
import { OutputThumbnail, StagePreview } from "./MediaPreview";
import {
  extractJobQualityScore,
  formatDate,
  formatPercent,
  formatScore,
  terminalStatuses,
  type GalleryItemResponse,
  type JobResponse,
} from "../studio";

type LatestJobPanelProps = {
  latestJob: JobResponse | null;
  onCancel: () => Promise<void>;
  jobAssets?: GalleryItemResponse[];
  selectedAssetId?: string | null;
  onSelectAsset?: (assetId: string) => void;
};

export function LatestJobPanel({
  latestJob,
  onCancel,
  jobAssets = [],
  selectedAssetId = null,
  onSelectAsset,
}: LatestJobPanelProps) {
  const cancelRequestInFlight = useRef(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const orderedJobAssets = [...jobAssets].sort(
    (left, right) =>
      (left.variation_index ?? Number.MAX_SAFE_INTEGER) -
      (right.variation_index ?? Number.MAX_SAFE_INTEGER),
  );
  const selectedJobAsset =
    orderedJobAssets.find((asset) => asset.asset_id === selectedAssetId) ??
    orderedJobAssets[0];

  async function handleCancel(): Promise<void> {
    if (cancelRequestInFlight.current) {
      return;
    }
    cancelRequestInFlight.current = true;
    setIsCancelling(true);
    try {
      await onCancel();
    } finally {
      cancelRequestInFlight.current = false;
      setIsCancelling(false);
    }
  }

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
            <button
              type="button"
              className="secondary-button"
              onClick={() => {
                void handleCancel();
              }}
              disabled={isCancelling}
              aria-busy={isCancelling}
            >
              {isCancelling ? "Cancelling job..." : "Cancel job"}
            </button>
          ) : null}
          <StagePreview
            mediaType={latestJob.media_type}
            outputPath={
              selectedJobAsset?.preview_path ??
              selectedJobAsset?.output_path ??
              latestJob.result?.previews[0] ??
              latestJob.result?.outputs[0] ??
              null
            }
            title={latestJob.request.prompt}
            subtitle={latestJob.request.model_id || "default"}
          />
          {orderedJobAssets.length > 1 ? (
            <div
              className="variation-comparison"
              role="group"
              aria-label="Generated variations"
            >
              <div className="variation-comparison__header">
                <strong>Compare variations</strong>
                <span>{orderedJobAssets.length} outputs</span>
              </div>
              <div className="variation-comparison__grid">
                {orderedJobAssets.map((asset, index) => {
                  const variationNumber = (asset.variation_index ?? index) + 1;
                  const isSelected = asset.asset_id === selectedJobAsset?.asset_id;
                  return (
                    <button
                      key={asset.asset_id}
                      type="button"
                      className={`variation-comparison__item ${
                        isSelected ? "is-selected" : ""
                      }`}
                      aria-pressed={isSelected}
                      aria-label={`Select variation ${variationNumber}${
                        asset.seed === null ? "" : `, seed ${asset.seed}`
                      }`}
                      onClick={() => onSelectAsset?.(asset.asset_id)}
                    >
                      <OutputThumbnail
                        mediaType={asset.media_type}
                        outputPath={asset.preview_path ?? asset.output_path}
                      />
                      <span>Variation {variationNumber}</span>
                      <small>
                        {asset.seed === null ? "Seed unavailable" : `Seed ${asset.seed}`}
                      </small>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}
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
