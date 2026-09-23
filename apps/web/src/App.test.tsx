import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { GalleryAssetDetailResponse } from "./studio";

const SOURCE_PROMPT = "時を巻き戻せる少女が雨の駅に立つ";

function makeVideoAsset(): GalleryAssetDetailResponse {
  return {
    asset_id: "asset_scene_1",
    job_id: "job_scene_1",
    project_id: "project_1",
    project_name: "Rewind",
    media_type: "video",
    prompt: SOURCE_PROMPT,
    model_id: "procedural-storyboard",
    output_path: "outputs/videos/scene_1.mp4",
    preview_path: null,
    created_at: "2026-09-23T00:00:00Z",
    updated_at: "2026-09-23T00:00:00Z",
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
    seed: 42,
    success: true,
    batch_id: null,
    batch_label: null,
    quality_report: {},
    request_snapshot: {
      media_type: "video",
      prompt: SOURCE_PROMPT,
      negative_prompt: null,
      model_id: "procedural-storyboard",
      seed: 42,
      output_format: "mp4",
      params: { story_id: "story_1", scene_id: "scene_1", duration_seconds: 4 },
    },
    metadata: {},
    feedback_summary: {},
    export_paths: [],
    parent_asset_id: null,
    lineage: [],
    tags: [],
  };
}

type RecordedRequest = { method: string; path: string; body: unknown };

/**
 * Stand in for the local API with just enough of each response for App to
 * render its gallery and asset detail. The Story and Matrix panels get empty
 * lists; anything else gets `[]`, which no panel reads during this flow.
 */
function stubStudioApi(asset: GalleryAssetDetailResponse): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  const json = (value: unknown) =>
    new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://studio.test");
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      requests.push({ method, path: url.pathname, body });

      if (url.pathname === "/health") {
        return json({ status: "ok" });
      }
      if (url.pathname === "/models") {
        return json({ models: [] });
      }
      if (url.pathname === "/catalog/loras") {
        return json({ items: [] });
      }
      if (url.pathname === "/metrics/summary") {
        return json({
          total_jobs: 1,
          succeeded_jobs: 1,
          failed_jobs: 0,
          running_jobs: 0,
          success_rate: 100,
          average_quality_score: null,
          average_semantic_alignment_score: null,
          average_creative_alignment_score: null,
          feedback_total: 0,
          feedback_coverage_rate: 0,
          by_media: {},
        });
      }
      if (url.pathname === "/gallery/stats") {
        return json({
          total_items: 1,
          total_by_media_type: { video: 1 },
          total_by_project: {},
          average_quality_score: null,
          total_reuse_count: 0,
          total_export_count: 0,
        });
      }
      if (url.pathname === "/gallery") {
        return json(url.searchParams.get("media_type") === asset.media_type ? [asset] : []);
      }
      if (url.pathname === `/gallery/${asset.asset_id}`) {
        return json(asset);
      }
      if (url.pathname === `/gallery/${asset.asset_id}/reuse` && method === "POST") {
        return json({
          asset_id: asset.asset_id,
          job_id: "job_rerun",
          status: "queued",
          project_id: asset.project_id,
        });
      }
      if (url.pathname === "/jobs/job_rerun") {
        return json({
          id: "job_rerun",
          media_type: asset.media_type,
          project_id: asset.project_id,
          status: "queued",
          progress: 0,
          error_message: null,
          request: asset.request_snapshot,
          result: null,
          created_at: "2026-09-23T00:00:01Z",
          updated_at: "2026-09-23T00:00:01Z",
        });
      }
      if (url.pathname === "/stories" || url.pathname === "/batches") {
        return json({ items: [] });
      }
      return json([]);
    }),
  );
  return requests;
}

function findReuseRequest(
  requests: RecordedRequest[],
  asset: GalleryAssetDetailResponse,
): RecordedRequest | undefined {
  return requests.find(
    (request) =>
      request.method === "POST" && request.path === `/gallery/${asset.asset_id}/reuse`,
  );
}

describe("Gallery Reuse and rerun", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("re-runs the selected asset's own request, not the composer's empty draft", async () => {
    // RC1 blocker: with the composer on the asset's media lane and its prompt
    // still empty (a Story-driven journey never touches the composer), the
    // button posted that empty draft and the job failed with
    // "Video prompt must not be empty."
    const asset = makeVideoAsset();
    const requests = stubStudioApi(asset);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Video workflows/ }));
    await user.click(await screen.findByRole("button", { name: "Reuse and rerun" }));

    await waitFor(() => {
      expect(findReuseRequest(requests, asset)).toBeDefined();
    });
    // No generation fields travel: the API fills prompt, params, model and
    // format from the asset's saved request and draws a fresh seed.
    expect(findReuseRequest(requests, asset)?.body).toEqual({
      action: "rerun",
      project_id: asset.project_id,
    });
    expect(
      await screen.findByText(`Created a fresh rerun from ${asset.asset_id}.`),
    ).toBeTruthy();
  });

  it("sends composer edits as a variation once the asset was loaded into the composer", async () => {
    const asset = makeVideoAsset();
    const requests = stubStudioApi(asset);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Video workflows/ }));
    await user.click(await screen.findByRole("button", { name: "Load into composer" }));
    const promptField = await screen.findByRole("textbox", { name: "Prompt" });
    await waitFor(() => {
      expect((promptField as HTMLTextAreaElement).value).toBe(SOURCE_PROMPT);
    });
    await user.clear(promptField);
    await user.type(promptField, "雨上がりの駅");
    await user.click(screen.getByRole("button", { name: "Reuse and rerun" }));

    await waitFor(() => {
      expect(findReuseRequest(requests, asset)).toBeDefined();
    });
    expect(findReuseRequest(requests, asset)?.body).toMatchObject({
      action: "variation",
      prompt: "雨上がりの駅",
      project_id: asset.project_id,
    });
  });
});
