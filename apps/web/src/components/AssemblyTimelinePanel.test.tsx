import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StoryDocument } from "../lib/storyApi";
import { AssemblyTimelinePanel } from "./AssemblyTimelinePanel";

function makeStory(overrides: Partial<StoryDocument> = {}): StoryDocument {
  return {
    id: "story_1",
    title: "Rewind",
    project_id: null,
    logline: "",
    premise: "時を巻き戻せる少女",
    language: "ja",
    format: "short-video",
    structure: "three-act",
    beats: [],
    scenes: [],
    chapters: [],
    metadata: {},
    source_job_ids: [],
    ...overrides,
  };
}

/** Route mocked fetch responses by URL substring and method. */
function stubFetch(routes: Array<[string, unknown, string?, number?]>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const match = routes.find(
      ([fragment, , routeMethod]) => url.includes(fragment) && (routeMethod ?? "GET") === method,
    );
    if (!match) {
      return new Response(JSON.stringify({ detail: `unrouted ${method} ${url}` }), {
        status: 404,
      });
    }
    return new Response(JSON.stringify(match[1]), { status: match[3] ?? 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AssemblyTimelinePanel", () => {
  it("shows a loading state while the story list is in flight", async () => {
    let resolveList: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveList = resolve;
          }),
      ),
    );

    render(<AssemblyTimelinePanel />);

    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText(/読み込んでいます/)).toBeTruthy();

    resolveList?.(
      new Response(JSON.stringify({ items: [], formats: ["short-video"] }), { status: 200 }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
    });
  });

  it("shows an empty state when there are no saved stories", async () => {
    stubFetch([["/stories", { items: [], formats: ["short-video"] }]]);
    render(<AssemblyTimelinePanel />);

    expect(await screen.findByText(/表示するストーリーがありません/)).toBeTruthy();
  });

  it("shows an empty state for a story that has no scenes yet", async () => {
    stubFetch([
      ["/stories/story_1", { story: makeStory(), missing_assets: [] }],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 0 }] }],
    ]);

    const user = userEvent.setup();
    render(<AssemblyTimelinePanel />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("表示するストーリー"), "story_1");

    expect(await screen.findByText(/並べるシーンがありません/)).toBeTruthy();
  });

  it("surfaces an error with a retry action when the story list fails to load", async () => {
    let callCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/stories")) {
          callCount += 1;
          if (callCount === 1) {
            return new Response(JSON.stringify({ detail: "boom" }), { status: 500 });
          }
          return new Response(JSON.stringify({ items: [], formats: ["short-video"] }), {
            status: 200,
          });
        }
        return new Response(JSON.stringify({ detail: "unrouted" }), { status: 404 });
      }),
    );

    const user = userEvent.setup();
    render(<AssemblyTimelinePanel />);

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("boom")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "再試行" }));

    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
    expect(callCount).toBe(2);
  });

  it("renders scene order, duration, and per-role assignment status without relying only on color", async () => {
    const story = makeStory({
      scenes: [
        {
          id: "scene_02",
          order: 1,
          heading: "夕暮れの駅",
          summary: "",
          narration: "",
          image_prompt: "sunset station",
          image_negative: "",
          bgm_mood: "wistful",
          duration_seconds: 6,
          camera: "",
          asset_ids: { visual: "asset_visual_2" },
        },
        {
          id: "scene_01",
          order: 0,
          heading: "屋上の朝",
          summary: "",
          narration: "朝の光が街を照らしていた。",
          image_prompt: "rooftop at dawn",
          image_negative: "",
          bgm_mood: "hopeful",
          duration_seconds: 5,
          camera: "",
          asset_ids: { visual: "asset_visual_1", narration: "asset_narration_1" },
        },
      ],
    });
    stubFetch([
      [
        "/stories/story_1",
        {
          story,
          missing_assets: [{ scene_id: "scene_02", role: "narration" }],
        },
      ],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 2 }] }],
    ]);

    const user = userEvent.setup();
    render(<AssemblyTimelinePanel />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("表示するストーリー"), "story_1");

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1); // drop header row
    expect(rows).toHaveLength(2);

    // Scenes render in `order`, not array position: scene_01 (order 0) is row 1.
    expect(within(rows[0]).getByText("屋上の朝")).toBeTruthy();
    expect(within(rows[0]).getByText("5.0 秒")).toBeTruthy();
    expect(within(rows[0]).getByText(/0\.0s.*5\.0s/)).toBeTruthy();

    expect(within(rows[1]).getByText("夕暮れの駅")).toBeTruthy();
    expect(within(rows[1]).getByText("6.0 秒")).toBeTruthy();
    expect(within(rows[1]).getByText(/5\.0s.*11\.0s/)).toBeTruthy();

    // Assigned role: shown with its asset id, distinct glyph, and no "missing" text.
    expect(
      within(rows[0]).getByText(/割り当て済み.*asset_narration_1/),
    ).toBeTruthy();

    // Required-but-missing role: distinguishable by both glyph and label text,
    // not only by a color class.
    const missingRow = within(rows[1]);
    expect(missingRow.getByText("未割り当て")).toBeTruthy();
    expect(missingRow.getByText("!")).toBeTruthy();

    // Optional, unset role (music is never required): a third, distinct state.
    expect(within(rows[0]).getAllByText("未設定（任意）").length).toBeGreaterThan(0);

    expect(screen.getByText(/2 シーン \/ 合計 11\.0 秒/)).toBeTruthy();
    expect(screen.getByText(/不足素材 1 件/)).toBeTruthy();
  });

  it("reports a complete timeline as fully ready with no outstanding state", async () => {
    const story = makeStory({
      scenes: [
        {
          id: "scene_01",
          order: 0,
          heading: "屋上の朝",
          summary: "",
          narration: "朝の光が街を照らしていた。",
          image_prompt: "rooftop at dawn",
          image_negative: "",
          bgm_mood: "hopeful",
          duration_seconds: 5,
          camera: "",
          asset_ids: {
            visual: "asset_visual_1",
            narration: "asset_narration_1",
            music: "asset_music_1",
          },
        },
      ],
    });
    stubFetch([
      [
        "/stories/story_1",
        {
          story,
          missing_assets: [],
          asset_status: [
            { scene_id: "scene_01", role: "visual", state: "assigned", required: true, asset_id: "asset_visual_1" },
            { scene_id: "scene_01", role: "narration", state: "assigned", required: true, asset_id: "asset_narration_1" },
            { scene_id: "scene_01", role: "music", state: "assigned", required: true, asset_id: "asset_music_1" },
          ],
        },
      ],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 1 }] }],
    ]);

    const user = userEvent.setup();
    render(<AssemblyTimelinePanel />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("表示するストーリー"), "story_1");

    await screen.findByRole("table");
    expect(screen.getByText(/素材はすべて揃っています/)).toBeTruthy();
    expect(screen.queryByText(/不足素材/)).toBeNull();
    expect(screen.queryByText(/生成中/)).toBeNull();
    expect(screen.queryByText(/失敗/)).toBeNull();
  });

  it("distinguishes a still-generating role from one that has never been attempted", async () => {
    const story = makeStory({
      scenes: [
        {
          id: "scene_01",
          order: 0,
          heading: "屋上の朝",
          summary: "",
          narration: "朝の光が街を照らしていた。",
          image_prompt: "rooftop at dawn",
          image_negative: "",
          bgm_mood: "hopeful",
          duration_seconds: 5,
          camera: "",
          asset_ids: { visual: "asset_visual_1" },
        },
      ],
    });
    stubFetch([
      [
        "/stories/story_1",
        {
          story,
          missing_assets: [
            { scene_id: "scene_01", role: "narration" },
            { scene_id: "scene_01", role: "music" },
          ],
          asset_status: [
            { scene_id: "scene_01", role: "visual", state: "assigned", required: true, asset_id: "asset_visual_1" },
            { scene_id: "scene_01", role: "narration", state: "generating", required: true, job_id: "job_narration" },
            { scene_id: "scene_01", role: "music", state: "missing", required: true },
          ],
        },
      ],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 1 }] }],
    ]);

    const user = userEvent.setup();
    render(<AssemblyTimelinePanel />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("表示するストーリー"), "story_1");

    const table = await screen.findByRole("table");
    const row = within(table).getAllByRole("row")[1];

    expect(within(row).getByText("生成中")).toBeTruthy();
    expect(within(row).getByText("…")).toBeTruthy();
    expect(within(row).getByText("未割り当て")).toBeTruthy();
    expect(within(row).getByText("!")).toBeTruthy();

    // The timeline-level summary counts both states, matching the per-scene
    // rows rather than collapsing them into one "missing" bucket.
    expect(screen.getByText(/不足素材 1 件/)).toBeTruthy();
    expect(screen.getByText(/生成中 1 件/)).toBeTruthy();
  });

  it("surfaces a failed generation with an accessible, collapsed reason", async () => {
    const story = makeStory({
      scenes: [
        {
          id: "scene_01",
          order: 0,
          heading: "屋上の朝",
          summary: "",
          narration: "朝の光が街を照らしていた。",
          image_prompt: "rooftop at dawn",
          image_negative: "",
          bgm_mood: "hopeful",
          duration_seconds: 5,
          camera: "",
          asset_ids: {},
        },
      ],
    });
    stubFetch([
      [
        "/stories/story_1",
        {
          story,
          missing_assets: [
            { scene_id: "scene_01", role: "visual" },
            { scene_id: "scene_01", role: "narration" },
          ],
          asset_status: [
            {
              scene_id: "scene_01",
              role: "visual",
              state: "failed",
              required: true,
              job_id: "job_visual",
              error_message: "no MusicGen weights installed",
            },
            { scene_id: "scene_01", role: "narration", state: "missing", required: true },
            { scene_id: "scene_01", role: "music", state: "missing", required: true },
          ],
        },
      ],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 1 }] }],
    ]);

    const user = userEvent.setup();
    render(<AssemblyTimelinePanel />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("表示するストーリー"), "story_1");

    const table = await screen.findByRole("table");
    const row = within(table).getAllByRole("row")[1];

    expect(within(row).getByText("生成失敗")).toBeTruthy();
    expect(within(row).getByText("✕")).toBeTruthy();
    expect(screen.getByText(/失敗 1 件/)).toBeTruthy();

    // The reason is present but collapsed by default — it does not
    // overwhelm the row — and reachable via the native, keyboard-operable
    // <details>/<summary> disclosure rather than a bespoke widget.
    const disclosure = within(row).getByText("失敗の理由").closest("details");
    expect(disclosure).toBeTruthy();
    expect(disclosure?.hasAttribute("open")).toBe(false);
    expect(within(row).getByText("no MusicGen weights installed")).toBeTruthy();
  });
});
