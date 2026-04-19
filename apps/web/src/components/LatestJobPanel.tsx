import Badge from "react-bootstrap/Badge";
import Stack from "react-bootstrap/Stack";
import type { GalleryAssetDetailResponse, JobResponse, JobStatus } from "../studio";
import {
  extractJobQualityScore,
  formatDate,
  formatJobStatus,
  formatPercent,
  formatScore,
  formatSemanticStatus,
} from "../studio";
import { StagePreview } from "./StagePreview";

type LatestJobPanelProps = {
  latestJob: JobResponse | null;
  projectName?: string | null;
  selectedAssetDetail?: GalleryAssetDetailResponse | null;
};

function getStatusVariant(status: JobStatus) {
  switch (status) {
    case "succeeded":
      return "success";
    case "failed":
      return "danger";
    case "running":
    case "preparing":
    case "postprocessing":
      return "primary";
    default:
      return "secondary";
  }
}

export function LatestJobPanel({
  latestJob,
  projectName,
  selectedAssetDetail = null,
}: LatestJobPanelProps) {
  return (
    <section className="review-section review-section--latest">
      <div className="review-section__header">
        <div>
          <p className="eyebrow">最新の生成</p>
          <h3>{projectName ? `${projectName} の最新結果` : "いま確認したい生成結果"}</h3>
        </div>
        {projectName ? <p className="section-footnote">このプロジェクトの直近の出力です。</p> : null}
      </div>

      {latestJob ? (
        <Stack gap={3}>
          <div className="review-status-row">
            <Badge bg={getStatusVariant(latestJob.status)}>{formatJobStatus(latestJob.status)}</Badge>
            <span className="section-footnote">{formatPercent(latestJob.progress * 100)}</span>
          </div>

          <StagePreview
            mediaType={latestJob.media_type}
            outputPath={latestJob.result?.previews[0] ?? latestJob.result?.outputs[0] ?? null}
            title={latestJob.request.prompt}
            subtitle={latestJob.request.model_id || "標準モデル"}
          />

          <div className="metadata-grid">
            <div className="metadata-item">
              <span>プロンプト</span>
              <strong>{latestJob.request.prompt}</strong>
            </div>
            <div className="metadata-item">
              <span>プロジェクト</span>
              <strong>{projectName ?? latestJob.project_id ?? "未割り当て"}</strong>
            </div>
            <div className="metadata-item">
              <span>自動品質</span>
              <strong>{formatScore(extractJobQualityScore(latestJob))}</strong>
            </div>
            <div className="metadata-item">
              <span>更新日時</span>
              <strong>{formatDate(latestJob.updated_at)}</strong>
            </div>
            <div className="metadata-item">
              <span>補正後品質</span>
              <strong>{formatScore(selectedAssetDetail?.quality_score_calibrated ?? null)}</strong>
            </div>
            <div className="metadata-item">
              <span>フィードバック</span>
              <strong>{selectedAssetDetail?.feedback_count ?? 0}</strong>
            </div>
            <div className="metadata-item">
              <span>semantic</span>
              <strong>{formatSemanticStatus(selectedAssetDetail?.semantic_status)}</strong>
            </div>
          </div>
        </Stack>
      ) : (
        <div className="empty-stage empty-stage--review">
          <div>
            <h3>まだ確認できる結果がありません</h3>
            <p>
              {projectName
                ? "このプロジェクトで生成を始めると、最新結果がここに表示されます。"
                : "中央から生成を始めると、直近の結果がここに表示されます。"}
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
