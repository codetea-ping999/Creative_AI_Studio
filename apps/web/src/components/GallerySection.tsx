import ListGroup from "react-bootstrap/ListGroup";
import type { GalleryItemResponse, MediaType } from "../studio";
import { formatScore, mediaTypeLabels } from "../studio";
import { OutputThumbnail } from "./StagePreview";

type GallerySectionProps = {
  galleryItems: GalleryItemResponse[];
  mediaType: MediaType;
  selectedAssetId: string | null;
  selectedProjectName?: string | null;
  selectedProjectAssetCount?: number;
  onSelectAsset: (assetId: string) => void;
};

export function GallerySection({
  galleryItems,
  mediaType,
  selectedAssetId,
  selectedProjectName,
  selectedProjectAssetCount = 0,
  onSelectAsset,
}: GallerySectionProps) {
  return (
    <section className="review-section review-section--gallery">
      <div className="review-section__header">
        <div>
          <p className="eyebrow">素材一覧</p>
          <h3>
            {selectedProjectName
              ? `${selectedProjectName} の${mediaTypeLabels[mediaType]}履歴`
              : `最近の${mediaTypeLabels[mediaType]}素材`}
          </h3>
        </div>
        <p className="section-footnote">
          {selectedProjectName
            ? `このプロジェクトの素材 ${selectedProjectAssetCount} 件を優先表示しています`
            : `${galleryItems.length} 件を表示しています`}
        </p>
      </div>

      {galleryItems.length > 0 ? (
        <ListGroup className="project-list gallery-list">
          {galleryItems.map((item) => (
            <ListGroup.Item
              key={item.asset_id}
              as="button"
              type="button"
              action
              active={item.asset_id === selectedAssetId}
              className="gallery-row"
              onClick={() => onSelectAsset(item.asset_id)}
            >
              <OutputThumbnail mediaType={item.media_type} outputPath={item.preview_path} />
              <div className="gallery-item__body">
                <div className="gallery-item__topline">
                  <span>{item.project_name || "未割り当て"}</span>
                  <span>{formatScore(item.quality_score_calibrated ?? item.quality_score)}</span>
                </div>
                <strong className="gallery-item__prompt" title={item.prompt}>
                  {item.prompt}
                </strong>
                <p
                  className="gallery-item__meta"
                  title={`${item.model_id} | フィードバック ${item.feedback_count} | 再利用 ${item.reuse_count} | 書き出し ${item.export_count}`}
                >
                  {item.model_id} | フィードバック {item.feedback_count} | 再利用{" "}
                  {item.reuse_count} | 書き出し {item.export_count}
                </p>
              </div>
            </ListGroup.Item>
          ))}
        </ListGroup>
      ) : (
        <div className="history-empty">
          結果が増えると、ここから次に確認したい素材を選べます。
        </div>
      )}
    </section>
  );
}
