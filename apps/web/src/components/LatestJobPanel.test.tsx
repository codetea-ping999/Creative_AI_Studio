import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GalleryItemResponse, JobResponse } from "../studio";
import { LatestJobPanel } from "./LatestJobPanel";

const runningJob: JobResponse = {
  id: "job-running",
  media_type: "image",
  project_id: null,
  status: "running",
  progress: 0.4,
  error_message: null,
  request: {
    media_type: "image",
    prompt: "A quiet workbench",
    negative_prompt: null,
    model_id: "sdxl",
    seed: null,
    output_format: "png",
    params: {},
  },
  result: null,
  created_at: "2026-07-26T00:00:00Z",
  updated_at: "2026-07-26T00:00:01Z",
};

const succeededJob: JobResponse = {
  ...runningJob,
  id: "job-variations",
  status: "succeeded",
  progress: 1,
  result: {
    outputs: ["/outputs/first.png", "/outputs/second.png"],
    previews: ["/outputs/first.png", "/outputs/second.png"],
    metadata: { base_seed: 42, variation_count: 2 },
  },
};

const baseAsset: GalleryItemResponse = {
  asset_id: "asset-first",
  job_id: succeededJob.id,
  project_id: null,
  project_name: null,
  media_type: "image",
  prompt: succeededJob.request.prompt,
  model_id: "sdxl",
  output_path: "/outputs/first.png",
  preview_path: "/outputs/first.png",
  created_at: succeededJob.created_at,
  updated_at: succeededJob.updated_at,
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
  variation_index: 0,
  seed: 42,
  success: true,
  batch_id: null,
  batch_label: null,
};

afterEach(() => {
  cleanup();
});

describe("LatestJobPanel", () => {
  it("prevents duplicate cancel requests while the first request is pending", async () => {
    const user = userEvent.setup();
    let finishCancel: (() => void) | undefined;
    const onCancel = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishCancel = resolve;
        }),
    );

    render(<LatestJobPanel latestJob={runningJob} onCancel={onCancel} />);

    await user.dblClick(screen.getByRole("button", { name: "Cancel job" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    const pendingButton = screen.getByRole("button", {
      name: "Cancelling job...",
    }) as HTMLButtonElement;
    expect(pendingButton.disabled).toBe(true);
    expect(pendingButton.getAttribute("aria-busy")).toBe("true");

    finishCancel?.();
    await waitFor(() => {
      expect(
        (screen.getByRole("button", { name: "Cancel job" }) as HTMLButtonElement)
          .disabled,
      ).toBe(false);
    });
  });

  it("compares generated variations and selects an individual asset", async () => {
    const user = userEvent.setup();
    const onSelectAsset = vi.fn();
    const jobAssets: GalleryItemResponse[] = [
      baseAsset,
      {
        ...baseAsset,
        asset_id: "asset-second",
        output_path: "/outputs/second.png",
        preview_path: "/outputs/second.png",
        variation_index: 1,
        seed: 43,
      },
    ];
    const { rerender } = render(
      <LatestJobPanel
        latestJob={succeededJob}
        onCancel={async () => undefined}
        jobAssets={jobAssets}
        selectedAssetId="asset-first"
        onSelectAsset={onSelectAsset}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Select variation 2, seed 43" }),
    );

    expect(onSelectAsset).toHaveBeenCalledWith("asset-second");
    rerender(
      <LatestJobPanel
        latestJob={succeededJob}
        onCancel={async () => undefined}
        jobAssets={jobAssets}
        selectedAssetId="asset-second"
        onSelectAsset={onSelectAsset}
      />,
    );
    expect(
      screen
        .getByRole("button", { name: "Select variation 2, seed 43" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });
});
