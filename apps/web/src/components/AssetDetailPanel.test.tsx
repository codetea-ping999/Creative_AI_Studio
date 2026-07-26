import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GalleryAssetDetailResponse } from "../studio";
import { AssetDetailPanel } from "./AssetDetailPanel";

const fluxDetail: GalleryAssetDetailResponse = {
  asset_id: "asset-flux",
  job_id: "job-flux",
  project_id: null,
  project_name: null,
  media_type: "image",
  prompt: "朝霧の苔庭に佇む茶室",
  model_id: "flux-dev",
  output_path: "/outputs/flux.png",
  preview_path: "/outputs/flux.png",
  created_at: "2026-07-26T00:00:00Z",
  updated_at: "2026-07-26T00:01:00Z",
  quality_score: 59.9,
  quality_level: "usable",
  semantic_alignment_score: null,
  creative_alignment_score: null,
  quality_score_calibrated: null,
  semantic_alignment_score_calibrated: null,
  creative_alignment_score_calibrated: null,
  feedback_count: 0,
  average_feedback_quality: null,
  reuse_count: 0,
  export_count: 0,
  variation_index: 0,
  seed: 21051205,
  success: true,
  quality_report: {},
  request_snapshot: {
    media_type: "image",
    prompt: "朝霧の苔庭に佇む茶室",
    negative_prompt: "文字、ぼやけ",
    model_id: "flux-dev",
    seed: 21051205,
    output_format: "png",
    params: {
      width: 512,
      height: 512,
      steps: 4,
      guidance_scale: 3.5,
    },
  },
  metadata: {
    pipeline_class: "FluxPipeline",
    device: "mps",
    load_dtype: "bfloat16",
    torch_dtype: "bfloat16",
    seed: 21051205,
    negative_prompt_applied: false,
    params: {
      width: 512,
      height: 512,
      num_inference_steps: 4,
      guidance_scale: 3.5,
    },
  },
  feedback_summary: {},
  export_paths: [],
  parent_asset_id: null,
  lineage: [],
  tags: [],
};

afterEach(() => {
  cleanup();
});

describe("AssetDetailPanel", () => {
  it("shows effective FLUX runtime and render metadata", () => {
    render(
      <AssetDetailPanel
        detail={fluxDetail}
        projects={[]}
        assetProjectId=""
        onAssetProjectIdChange={vi.fn()}
        isAssetBusy={false}
        isFeedbackBusy={false}
        onOpenQuickReview={vi.fn()}
        onQuickReview={async () => true}
        onReuse={vi.fn()}
        onLoadIntoComposer={vi.fn()}
        onExport={vi.fn()}
        onBindProject={vi.fn()}
        onSubmitFeedback={async () => true}
      />,
    );

    const heading = screen.getByRole("heading", {
      name: "Effective runtime and render settings",
    });
    const section = heading.closest(".form-section");
    expect(section).not.toBeNull();
    const metadata = within(section as HTMLElement);
    expect(metadata.getByText("FluxPipeline")).toBeTruthy();
    expect(metadata.getByText("mps")).toBeTruthy();
    expect(metadata.getByText("bfloat16")).toBeTruthy();
    expect(metadata.getByText("21051205")).toBeTruthy();
    expect(metadata.getByText("512 × 512")).toBeTruthy();
    expect(metadata.getByText("Not applied (FLUX)")).toBeTruthy();
  });
});
