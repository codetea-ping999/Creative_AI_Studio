import { useRef, useState } from "react";
import { OutputThumbnail, StagePreview } from "./MediaPreview";
import {
  extractJobQualityScore,
  formatDate,
  formatPercent,
  formatScore,
  isPlayableVideoAsset,
  terminalStatuses,
  type GalleryItemResponse,
  type JobResponse,
} from "../studio";
import { jobStatusLabel } from "../lib/jobStatus";

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
  // #447: an Assembly MP4's preview is a still `*_preview.png`; play the output
  // itself and use the still as the poster frame.
  const stageOutputPath = selectedJobAsset
    ? selectedJobAsset.output_path
    : (latestJob?.result?.outputs[0] ?? null);
  const stagePosterPath = selectedJobAsset
    ? selectedJobAsset.preview_path
    : (latestJob?.result?.previews[0] ?? null);
  const stageVideoPath = isPlayableVideoAsset(stageOutputPath) ? stageOutputPath : null;

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
          <p className="eyebrow">最新のジョブ</p>
          <h2>進み具合と結果</h2>
        </div>
      </div>
      {latestJob ? (
        <div className="monitor-stack">
          <div className="gallery-item__topline">
            <span className={`status-chip status-chip--${latestJob.status}`} role="status">
              {jobStatusLabel(latestJob.status)}
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
              {isCancelling ? "中断しています..." : "中断する"}
            </button>
          ) : null}
          <StagePreview
            mediaType={latestJob.media_type}
            outputPath={
              stageVideoPath ??
              selectedJobAsset?.preview_path ??
              selectedJobAsset?.output_path ??
              latestJob.result?.previews[0] ??
              latestJob.result?.outputs[0] ??
              null
            }
            posterPath={stageVideoPath ? stagePosterPath : null}
            title={latestJob.request.prompt}
            subtitle={latestJob.request.model_id || "default"}
          />
          {orderedJobAssets.length > 1 ? (
            <div
              className="variation-comparison"
              role="group"
              aria-label="作った別案"
            >
              <div className="variation-comparison__header">
                <strong>別案を見比べる</strong>
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
                        {asset.seed === null ? "シード不明" : `シード ${asset.seed}`}
                      </small>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}
          <div className="metadata-grid">
            <div className="metadata-item">
              <span>プロンプト</span>
              <strong>{latestJob.request.prompt}</strong>
            </div>
            <div className="metadata-item">
              <span>プロジェクト</span>
              <strong>{latestJob.project_id ?? "プロジェクトなし"}</strong>
            </div>
            <div className="metadata-item">
              <span>品質</span>
              <strong>{formatScore(extractJobQualityScore(latestJob))}</strong>
            </div>
            <div className="metadata-item">
              <span>更新</span>
              <strong>{formatDate(latestJob.updated_at)}</strong>
            </div>
          </div>
        </div>
      ) : (
        <div className="empty-stage">
          <div>
            <h3>まだ生成していません</h3>
            <p>上の入力欄から生成すると、進み具合と結果がここに出ます。</p>
          </div>
        </div>
      )}
    </section>
  );
}

export default LatestJobPanel;
