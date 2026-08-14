import { useMemo, useState } from "react";
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
  activeBatchId: string | null;
  onFilterByBatch: (batchId: string | null) => void;
};

type GalleryRow =
  | { kind: "single"; item: GalleryItemResponse }
  | { kind: "batch"; batchId: string; label: string; items: GalleryItemResponse[] };

/** Fold same-batch items into one row so a 30-pattern sweep reads as one card. */
function groupByBatch(items: GalleryItemResponse[]): GalleryRow[] {
  const rows: GalleryRow[] = [];
  const batchRowIndex = new Map<string, number>();

  for (const item of items) {
    if (!item.batch_id) {
      rows.push({ kind: "single", item });
      continue;
    }
    const existingIndex = batchRowIndex.get(item.batch_id);
    if (existingIndex === undefined) {
      batchRowIndex.set(item.batch_id, rows.length);
      rows.push({
        kind: "batch",
        batchId: item.batch_id,
        label: item.prompt,
        items: [item],
      });
    } else {
      const row = rows[existingIndex];
      if (row.kind === "batch") {
        row.items.push(item);
      }
    }
  }
  return rows;
}

export function GalleryPanel({
  mediaLabel,
  items,
  projectName,
  search,
  onSearchChange,
  selectedAssetId,
  onSelect,
  disabled,
  activeBatchId,
  onFilterByBatch,
}: GalleryPanelProps) {
  const [expandedBatchIds, setExpandedBatchIds] = useState<Set<string>>(new Set());
  const rows = useMemo(() => groupByBatch(items), [items]);

  function toggleExpanded(batchId: string): void {
    setExpandedBatchIds((current) => {
      const next = new Set(current);
      if (next.has(batchId)) {
        next.delete(batchId);
      } else {
        next.add(batchId);
      }
      return next;
    });
  }

  function renderItemButton(item: GalleryItemResponse, indented: boolean) {
    return (
      <button
        key={item.asset_id}
        type="button"
        className={`gallery-item ${item.asset_id === selectedAssetId ? "is-active" : ""} ${
          indented ? "gallery-item--nested" : ""
        }`}
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
          <strong>{item.batch_label || item.prompt}</strong>
          <p>
            {item.model_id} | feedback {item.feedback_count} | reuse {item.reuse_count} |
            export {item.export_count}
          </p>
        </div>
      </button>
    );
  }

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
      {activeBatchId ? (
        <div className="gallery-batch-filter">
          <span>Showing one batch only</span>
          <button type="button" className="secondary-button secondary-button--chip" onClick={() => onFilterByBatch(null)}>
            Clear batch filter ×
          </button>
        </div>
      ) : null}
      <div className="gallery-list">
        {rows.length > 0 ? (
          rows.map((row) => {
            if (row.kind === "single") {
              return renderItemButton(row.item, false);
            }
            const isExpanded = expandedBatchIds.has(row.batchId);
            const representative = row.items[0];
            return (
              <div className="gallery-batch-group" key={row.batchId}>
                <button
                  type="button"
                  className="gallery-item gallery-item--batch"
                  onClick={() => toggleExpanded(row.batchId)}
                  aria-expanded={isExpanded}
                >
                  <OutputThumbnail
                    mediaType={representative.media_type}
                    outputPath={representative.preview_path}
                  />
                  <div className="gallery-item__body">
                    <div className="gallery-item__topline">
                      <span className="history-item__media">Batch · {row.items.length} items</span>
                      <span className="history-score">
                        {formatScore(
                          representative.quality_score_calibrated ?? representative.quality_score,
                        )}
                      </span>
                    </div>
                    <strong>{row.label}</strong>
                    <p>{isExpanded ? "Tap to collapse" : "Tap to expand this batch"}</p>
                  </div>
                </button>
                {isExpanded ? (
                  <div className="gallery-batch-group__items">
                    <button
                      type="button"
                      className="secondary-button secondary-button--chip"
                      onClick={() => onFilterByBatch(row.batchId)}
                    >
                      Show only this batch
                    </button>
                    {row.items.map((item) => renderItemButton(item, true))}
                  </div>
                ) : null}
              </div>
            );
          })
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
