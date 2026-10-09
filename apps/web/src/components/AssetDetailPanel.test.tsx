import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GalleryAssetDetailResponse } from "../studio";
import { AssetDetailPanel } from "./AssetDetailPanel";

const assemblyDetail: GalleryAssetDetailResponse = {
  asset_id: "asset-assembly",
  job_id: "job-assembly",
  project_id: null,
  project_name: null,
  media_type: "video",
  prompt: "Assembled story",
  model_id: "assembly",
  output_path: "outputs/videos/job-assembly.mp4",
  preview_path: "outputs/videos/job-assembly_preview.png",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:01Z",
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
  quality_report: {},
  request_snapshot: {
    media_type: "video",
    prompt: "Assembled story",
    negative_prompt: null,
    model_id: "assembly",
    seed: null,
    output_format: "mp4",
    params: {},
  },
  metadata: {},
  feedback_summary: {},
  export_paths: [],
  parent_asset_id: null,
  lineage: [],
  tags: [],
};

function renderPanel(detail: GalleryAssetDetailResponse) {
  return render(
    <AssetDetailPanel
      detail={detail}
      projects={[]}
      assetProjectId=""
      onAssetProjectIdChange={vi.fn()}
      isAssetBusy={false}
      isFeedbackBusy={false}
      onOpenQuickReview={vi.fn()}
      onQuickReview={vi.fn(async () => true)}
      onReuse={vi.fn()}
      canConditionMelody={false}
      melodyConditioningMessage=""
      onConditionMelody={vi.fn()}
      onLoadIntoComposer={vi.fn()}
      onExport={vi.fn()}
      onBindProject={vi.fn()}
      onSubmitFeedback={vi.fn(async () => true)}
    />,
  );
}

afterEach(() => {
  cleanup();
});

describe("AssetDetailPanel stage preview", () => {
  // #447: an Assembly MP4's preview_path is a still PNG, so the Gallery detail
  // showed only a still instead of a playable video.
  it("plays an Assembly MP4 with its still preview as the poster", () => {
    const { container } = renderPanel(assemblyDetail);

    const video = container.querySelector(".stage-surface video");
    expect(video).not.toBeNull();
    expect(video?.hasAttribute("controls")).toBe(true);
    expect(video?.getAttribute("src")).toMatch(/\/outputs\/videos\/job-assembly\.mp4$/);
    expect(video?.getAttribute("poster")).toMatch(
      /\/outputs\/videos\/job-assembly_preview\.png$/,
    );
    expect(container.querySelector(".stage-surface img")).toBeNull();
  });

  it("keeps a storyboard GIF as an image", () => {
    const { container } = renderPanel({
      ...assemblyDetail,
      output_path: "outputs/videos/storyboard_scene_1.gif",
      preview_path: "outputs/videos/storyboard_scene_1.gif",
    });

    expect(container.querySelector(".stage-surface video")).toBeNull();
    expect(container.querySelector(".stage-surface img")?.getAttribute("src")).toMatch(
      /storyboard_scene_1\.gif$/,
    );
  });

  it("keeps showing the preview path for images", () => {
    const { container } = renderPanel({
      ...assemblyDetail,
      media_type: "image",
      output_path: "outputs/images/full.png",
      preview_path: "outputs/images/full_preview.png",
    });

    expect(container.querySelector(".stage-surface video")).toBeNull();
    expect(container.querySelector(".stage-surface img")?.getAttribute("src")).toMatch(
      /full_preview\.png$/,
    );
  });
});
