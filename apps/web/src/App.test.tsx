import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { LOCALE_STORAGE_KEY } from "./i18n";
import { LocaleProvider } from "./i18n/LocaleProvider";
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
        // Only the weight-free storyboard runtime is installed, as on a fresh
        // checkout: image and audio have nothing usable.
        const mediaType = url.searchParams.get("media_type");
        return json({
          models:
            mediaType === "video"
              ? [
                  {
                    id: "storyboard-video",
                    display_name: "Storyboard Video",
                    media_type: "video",
                    default_params: {},
                    tags: [],
                    is_default: true,
                    is_available: true,
                    runtime_status: "ready",
                    availability_message: "",
                  },
                ]
              : mediaType === "image"
                ? [
                    {
                      id: "sdxl",
                      display_name: "SDXL Local",
                      media_type: "image",
                      default_params: {},
                      tags: [],
                      is_default: true,
                      is_available: false,
                      runtime_status: "missing_files",
                      availability_message: "Diffusers model files are missing",
                    },
                  ]
                : [],
        });
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
    window.localStorage.clear();
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

    await user.click(await screen.findByRole("button", { name: "動画" }));
    await user.click(await screen.findByRole("button", { name: "同じ条件で作り直す" }));

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
      await screen.findByText("選んだ成果物と同じ条件で作り直します。"),
    ).toBeTruthy();
  });

  it("sends composer edits as a variation once the asset was loaded into the composer", async () => {
    const asset = makeVideoAsset();
    const requests = stubStudioApi(asset);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "動画" }));
    await user.click(await screen.findByRole("button", { name: "入力欄に読み込む" }));
    const promptField = await screen.findByRole("textbox", { name: "プロンプト" });
    await waitFor(() => {
      expect((promptField as HTMLTextAreaElement).value).toBe(SOURCE_PROMPT);
    });
    await user.clear(promptField);
    await user.type(promptField, "雨上がりの駅");
    await user.click(screen.getByRole("button", { name: "同じ条件で作り直す" }));

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

describe("Studio screens", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.location.hash = "";
    window.localStorage.clear();
  });

  it("shows one screen at a time and keeps it in the URL hash", async () => {
    stubStudioApi(makeVideoAsset());
    const user = userEvent.setup();
    render(<App />);

    const nav = await screen.findByRole("navigation", { name: "画面" });
    expect(within(nav).getByRole("link", { name: /つくる/ }).getAttribute("aria-current")).toBe(
      "page",
    );
    expect(screen.queryByRole("button", { name: "ストーリーを作成" })).toBeNull();

    await user.click(within(nav).getByRole("link", { name: /ストーリー/ }));

    expect(window.location.hash).toBe("#/story");
    expect(await screen.findByRole("button", { name: "ストーリーを作成" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "プロンプト" })).toBeNull();
  });

  it("opens the screen named in the hash on load", async () => {
    window.location.hash = "#/setup";
    stubStudioApi(makeVideoAsset());
    render(<App />);

    expect(await screen.findByRole("heading", { level: 1, name: "準備" })).toBeTruthy();
    // Project management and theme live on the setup screen; choosing where
    // new work is saved stays in the top bar on every screen.
    expect(screen.getByRole("heading", { name: "プロジェクト" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "画面の明るさ" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "保存先" })).toBeTruthy();
  });

  it("switches the interface to English from the setup screen and remembers it", async () => {
    window.location.hash = "#/setup";
    stubStudioApi(makeVideoAsset());
    const user = userEvent.setup();
    render(
      <LocaleProvider>
        <App />
      </LocaleProvider>,
    );

    const language = await screen.findByRole("group", { name: "言語" });
    await user.click(within(language).getByRole("button", { name: "English" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Setup" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Language" })).toBeTruthy();
    expect(
      within(screen.getByRole("group", { name: "Language" }))
        .getByRole("button", { name: "English" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(document.documentElement.lang).toBe("en");
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("en");

    await user.click(screen.getByRole("button", { name: "日本語" }));
    expect(await screen.findByRole("heading", { level: 1, name: "準備" })).toBeTruthy();
  });
});

describe("Model readiness", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.location.hash = "";
    window.localStorage.clear();
  });

  it("opens on a media type that can generate when nobody chose one", async () => {
    stubStudioApi(makeVideoAsset());
    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /動画/ }).getAttribute("aria-pressed")).toBe(
        "true",
      );
    });
    expect(screen.getByRole("button", { name: /画像/ }).textContent).toContain("準備が必要");
  });

  it("replaces the composer with one setup card for a media type without models", async () => {
    window.localStorage.setItem("creative-ai-studio.media-type", "image");
    stubStudioApi(makeVideoAsset());
    const user = userEvent.setup();
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "画像のモデルがまだ置かれていません" }),
    ).toBeTruthy();
    expect(screen.getByText("SDXL Local")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "プロンプト" })).toBeNull();

    await user.click(await screen.findByRole("button", { name: "動画をつくる" }));
    expect(await screen.findByRole("textbox", { name: "プロンプト" })).toBeTruthy();
    expect(window.localStorage.getItem("creative-ai-studio.media-type")).toBe("video");
  });

  it("lists every media type's readiness on the setup screen", async () => {
    window.location.hash = "#/setup";
    stubStudioApi(makeVideoAsset());
    render(<App />);

    const list = await screen.findByRole("heading", { name: "この端末で使えるもの" });
    const section = list.closest("section") as HTMLElement;
    await waitFor(() => {
      expect(within(section).getByText("Storyboard Video")).toBeTruthy();
    });
    expect(within(section).getAllByText(/準備が必要/).length).toBe(2);
    expect(within(section).getByText("未配置のモデル 1 件と配置先")).toBeTruthy();
  });
});

describe("Library screen", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.location.hash = "";
    window.localStorage.clear();
  });

  it("lists past assets with their detail, apart from the composer", async () => {
    window.localStorage.setItem("creative-ai-studio.media-type", "video");
    window.location.hash = "#/library";
    stubStudioApi(makeVideoAsset());
    render(<App />);

    expect(await screen.findByRole("heading", { name: "判定・再利用・書き出し" })).toBeTruthy();
    expect(await screen.findByRole("button", { name: "同じ条件で作り直す" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "プロンプト" })).toBeNull();
  });
});
