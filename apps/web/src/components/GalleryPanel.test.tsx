import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GalleryItemResponse } from "../studio";
import { GalleryPanel } from "./GalleryPanel";

const baseAsset: GalleryItemResponse = {
  asset_id: "asset-standalone",
  job_id: "job-standalone",
  project_id: null,
  project_name: null,
  media_type: "text",
  prompt: "a lone standing job",
  model_id: "template-writer",
  output_path: "/outputs/standalone.md",
  preview_path: "/outputs/standalone.md",
  created_at: "2026-07-26T00:00:00Z",
  updated_at: "2026-07-26T00:00:01Z",
  quality_score: null,
  quality_level: null,
  semantic_alignment_score: null,
  creative_alignment_score: null,
  quality_score_calibrated: null,
  semantic_alignment_score_calibrated: null,
  creative_alignment_score_calibrated: null,
  feedback_count: 0,
  average_feedback_quality: null,
  reuse_count: 0,
  export_count: 0,
  variation_index: null,
  seed: null,
  success: true,
  batch_id: null,
  batch_label: null,
};

function batchItem(overrides: Partial<GalleryItemResponse>): GalleryItemResponse {
  return {
    ...baseAsset,
    asset_id: overrides.asset_id ?? "asset-batch",
    job_id: overrides.asset_id ?? "job-batch",
    prompt: "acme coffee roasters logo",
    batch_id: "batch-1",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

const noop = () => undefined;

describe("GalleryPanel", () => {
  it("folds same-batch items into one row and keeps standalone items separate", () => {
    const items: GalleryItemResponse[] = [
      batchItem({ asset_id: "asset-a", batch_label: "tense" }),
      batchItem({ asset_id: "asset-b", batch_label: "playful" }),
      baseAsset,
    ];

    render(
      <GalleryPanel
        mediaLabel="Text"
        items={items}
        projectName={null}
        search=""
        onSearchChange={noop}
        selectedAssetId={null}
        onSelect={noop}
        disabled={false}
        activeBatchId={null}
        onFilterByBatch={noop}
      />,
    );

    expect(screen.getByText("Batch · 2 items")).toBeTruthy();
    expect(screen.getByText("a lone standing job")).toBeTruthy();
    expect(screen.queryByText("tense")).toBeNull();
  });

  it("expands a batch row to reveal its items and can filter to just that batch", async () => {
    const user = userEvent.setup();
    const onFilterByBatch = vi.fn();
    const onSelect = vi.fn();
    const items: GalleryItemResponse[] = [
      batchItem({ asset_id: "asset-a", batch_label: "tense" }),
      batchItem({ asset_id: "asset-b", batch_label: "playful" }),
    ];

    render(
      <GalleryPanel
        mediaLabel="Text"
        items={items}
        projectName={null}
        search=""
        onSearchChange={noop}
        selectedAssetId={null}
        onSelect={onSelect}
        disabled={false}
        activeBatchId={null}
        onFilterByBatch={onFilterByBatch}
      />,
    );

    await user.click(screen.getByText("Batch · 2 items"));
    expect(screen.getByText("tense")).toBeTruthy();
    expect(screen.getByText("playful")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Show only this batch" }));
    expect(onFilterByBatch).toHaveBeenCalledWith("batch-1");

    await user.click(screen.getByText("tense"));
    expect(onSelect).toHaveBeenCalledWith("asset-a");
  });

  it("shows a clear-filter chip when a batch filter is active", async () => {
    const user = userEvent.setup();
    const onFilterByBatch = vi.fn();

    render(
      <GalleryPanel
        mediaLabel="Text"
        items={[batchItem({ asset_id: "asset-a" })]}
        projectName={null}
        search=""
        onSearchChange={noop}
        selectedAssetId={null}
        onSelect={noop}
        disabled={false}
        activeBatchId="batch-1"
        onFilterByBatch={onFilterByBatch}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Clear batch filter/ }));
    expect(onFilterByBatch).toHaveBeenCalledWith(null);
  });
});
