import { OutputThumbnail } from "./MediaPreview";
import { formatScore, type GalleryItemResponse } from "../studio";

type GalleryPanelProps = {
  mediaLabel: string;
  items: GalleryItemResponse[];
  projectName: string | null;
  search: string;
  onSearchChange: (value: string) => void;
  selectedAssetId: string | null;
  onSelect: (assetId: string) => void;
  disabled: boolean;
};

export function GalleryPanel({
  mediaLabel,
  items,
  projectName,
  search,
  onSearchChange,
  selectedAssetId,
  onSelect,
  disabled,
}: GalleryPanelProps) {
  return (
    <section className="section-card section-card--gallery">
      <div className="section-card__header">
        <div>
          <p className="eyebrow">Gallery</p>
          <h2>Recent {mediaLabel.toLowerCase()} assets</h2>
        </div>
        <p className="section-footnote">
          {items.length} items loaded
          {projectName ? ` | ${projectName}` : ""}
        </p>
      </div>
      <label className="field-group field-group--full">
        <span>Search current gallery</span>
        <input
          type="search"
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="prompt, model, project, metadata"
        />
      </label>
      <div className="gallery-list">
        {items.length > 0 ? (
          items.map((item) => (
            <button
              key={item.asset_id}
              type="button"
              className={`gallery-item ${item.asset_id === selectedAssetId ? "is-active" : ""}`}
              onClick={() => onSelect(item.asset_id)}
              disabled={disabled}
            >
              <OutputThumbnail mediaType={item.media_type} outputPath={item.preview_path} />
              <div className="gallery-item__body">
                <div className="gallery-item__topline">
                  <span className="history-item__media">{item.project_name || "Unassigned"}</span>
                  <span className="history-score">
                    {formatScore(item.quality_score_calibrated ?? item.quality_score)}
                  </span>
                </div>
                <strong>{item.prompt}</strong>
                <p>
                  {item.model_id} | feedback {item.feedback_count} | reuse {item.reuse_count} |
                  export {item.export_count}
                </p>
              </div>
            </button>
          ))
        ) : (
          <div className="history-empty">
            Successful jobs will appear here after the runner finishes them.
          </div>
        )}
      </div>
    </section>
  );
}

export default GalleryPanel;
