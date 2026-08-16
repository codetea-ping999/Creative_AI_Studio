import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MatrixPanel } from "./components/MatrixPanel";
import { StoryPanel } from "./components/StoryPanel";
import {
  availableStages,
  isReadyToAssemble,
  loglineCandidates,
  missingRolesForScene,
  resolveSceneTarget,
  type StoryDocument,
  type StoryScene,
} from "./lib/storyApi";
import {
  batchProgressLabel,
  currentStageItems,
  type Batch,
  type BatchItem,
} from "./lib/batchApi";

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

function makeItem(overrides: Partial<BatchItem> = {}): BatchItem {
  return {
    id: "item_0",
    index: 0,
    label: "centered-wordmark",
    stage_name: "probe",
    stage_index: 0,
    axis_values: { logo_structure: "centered-wordmark" },
    job_id: "job_0",
    status: "succeeded",
    score: 70,
    output_path: "outputs/images/a.png",
    preview_path: "outputs/images/a.png",
    error_message: null,
    promoted: false,
    ...overrides,
  };
}

function makeBatch(overrides: Partial<Batch> = {}): Batch {
  return {
    id: "batch_1",
    name: "Logo 30 patterns",
    media_type: "image",
    project_id: null,
    status: "running",
    stage_index: 0,
    stage_names: ["probe", "refine"],
    aggregate: {
      total: 2,
      pending: 0,
      running: 1,
      succeeded: 1,
      failed: 0,
      cancelled: 0,
      average_score: 70,
      best_item_id: "item_0",
    },
    items: [makeItem()],
    created_at: "2026-07-26T00:00:00+00:00",
    updated_at: "2026-07-26T00:00:00+00:00",
    ...overrides,
  };
}

/** Route mocked fetch responses by URL substring and method. */
function stubFetch(routes: Array<[string, unknown, string?]>) {
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(String(init.body)) : null,
    });
    const match = routes.find(
      ([fragment, , routeMethod]) =>
        url.includes(fragment) && (routeMethod ?? "GET") === method,
    );
    if (!match) {
      return new Response(JSON.stringify({ detail: `unrouted ${method} ${url}` }), {
        status: 404,
      });
    }
    return new Response(JSON.stringify(match[1]), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("story stage gating", () => {
  it("only unlocks a stage once its input exists", () => {
    expect(availableStages(null).size).toBe(0);

    const fresh = makeStory();
    expect([...availableStages(fresh)]).toEqual(["logline"]);

    const withLogline = makeStory({ logline: "a girl rewinds a day" });
    expect(availableStages(withLogline).has("beat_sheet")).toBe(true);
    expect(availableStages(withLogline).has("scene_list")).toBe(false);

    const withBeats = makeStory({
      logline: "l",
      beats: [{ id: "beat_01", act: "1", purpose: "p", summary: "s", order: 0 }],
    });
    expect(availableStages(withBeats).has("scene_list")).toBe(true);
    expect(availableStages(withBeats).has("prose")).toBe(false);
  });

  it("treats an empty story with no premise as having nothing to write", () => {
    const empty = makeStory({ premise: "", title: "", logline: "" });
    expect(availableStages(empty).size).toBe(0);
  });

  it("keeps the script target on a scene that still exists", () => {
    expect(resolveSceneTarget(null, "scene_02")).toBe("");
    expect(resolveSceneTarget(makeStory(), "scene_02")).toBe("");

    const withScenes = makeStory({
      scenes: [
        makeScene({ id: "scene_01", order: 0 }),
        makeScene({ id: "scene_02", order: 1 }),
      ],
    });
    expect(resolveSceneTarget(withScenes, "scene_02")).toBe("scene_02");
    // A regenerated scene list can drop the selection; the first scene keeps the
    // stage reachable instead of leaving it disabled.
    expect(resolveSceneTarget(withScenes, "scene_09")).toBe("scene_01");
    expect(resolveSceneTarget(withScenes, "")).toBe("scene_01");
  });

  it("reads logline candidates defensively", () => {
    expect(loglineCandidates(null)).toEqual([]);
    expect(
      loglineCandidates(
        makeStory({ metadata: { logline_candidates: [{ text: "a" }, { nope: 1 }] } }),
      ),
    ).toEqual(["a"]);
  });
});

describe("batch comparison helpers", () => {
  it("ranks the current stage by score and keeps index order on ties", () => {
    const batch = makeBatch({
      stage_index: 1,
      items: [
        makeItem({ id: "old", stage_index: 0, score: 99 }),
        makeItem({ id: "b", stage_index: 1, index: 1, score: 50 }),
        makeItem({ id: "a", stage_index: 1, index: 2, score: 80 }),
        makeItem({ id: "c", stage_index: 1, index: 3, score: 50 }),
        makeItem({ id: "none", stage_index: 1, index: 4, score: null }),
      ],
    });
    // The earlier stage is excluded: a 640px probe and a 1024px render are not
    // comparable on the same score.
    expect(currentStageItems(batch).map((item) => item.id)).toEqual([
      "a",
      "b",
      "c",
      "none",
    ]);
  });

  it("summarises progress including failures", () => {
    const label = batchProgressLabel(
      makeBatch({
        aggregate: {
          total: 30,
          pending: 10,
          running: 5,
          succeeded: 12,
          failed: 3,
          cancelled: 0,
          average_score: 64.2,
          best_item_id: "item_0",
        },
      }),
    );
    expect(label).toContain("probe");
    expect(label).toContain("15/30");
    expect(label).toContain("失敗 3");
  });
});

describe("StoryPanel", () => {
  beforeEach(() => {
    stubFetch([["/stories", { items: [], formats: ["short-video"] }]]);
  });

  it("shows an empty state until a story is selected", async () => {
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);
    expect(
      await screen.findByText(/ストーリーを作成するか/),
    ).toBeTruthy();
  });

  it("keeps the create action disabled until there is something to write about", async () => {
    const user = userEvent.setup();
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);

    const createButton = screen.getByRole("button", { name: "ストーリーを作成" });
    expect(createButton.hasAttribute("disabled")).toBe(true);

    await user.type(screen.getByLabelText("前提（premise）"), "時を巻き戻す少女");
    expect(createButton.hasAttribute("disabled")).toBe(false);
  });

  it("renders scenes with their asset status and surfaces the shortfall", async () => {
    const story = makeStory({
      logline: "a girl rewinds a day",
      beats: [{ id: "beat_01", act: "1", purpose: "p", summary: "s", order: 0 }],
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
      ["/stories/story_1", { story, missing_assets: [{ scene_id: "scene_01", role: "visual" }] }],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 1 }] }],
    ]);

    const user = userEvent.setup();
    const onGenerate = vi.fn();
    render(
      <StoryPanel
        modelId=""
        awaitJob={async () => "succeeded"}
        onGenerateSceneImage={onGenerate}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(
      screen.getByLabelText("編集中のストーリー"),
      "story_1",
    );

    const table = await screen.findByRole("table");
    expect(within(table).getByText("屋上の朝")).toBeTruthy();
    expect(within(table).getByText(/不足: visual/)).toBeTruthy();
    expect(screen.getByText(/不足素材 1 件/)).toBeTruthy();

    await user.click(
      within(table).getByRole("button", { name: "画像をコンポーザへ" }),
    );
    expect(onGenerate).toHaveBeenCalledWith(
      expect.objectContaining({ id: "scene_01", image_prompt: "rooftop at dawn" }),
    );
  });

  it("disables stages whose input has not been written yet", async () => {
    const story = makeStory();
    stubFetch([
      ["/stories/story_1", { story, missing_assets: [] }],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 0 }] }],
    ]);

    const user = userEvent.setup();
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("編集中のストーリー"), "story_1");

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Logline" }).hasAttribute("disabled"),
      ).toBe(false);
    });
    expect(screen.getByRole("button", { name: "Scenes" }).hasAttribute("disabled")).toBe(
      true,
    );
  });

  it("writes the script into the scene the user picked", async () => {
    const story = makeStory({
      logline: "a girl rewinds a day",
      beats: [{ id: "beat_01", act: "1", purpose: "p", summary: "s", order: 0 }],
      scenes: [
        makeScene({ id: "scene_01", order: 0, heading: "屋上の朝" }),
        makeScene({ id: "scene_02", order: 1, heading: "路地の追跡" }),
      ],
    });
    const calls = stubFetch([
      ["/stories/story_1/expand", { job_id: "job_1", status: "queued" }, "POST"],
      ["/stories/story_1/apply", { story, missing_assets: [] }, "POST"],
      ["/stories/story_1", { story, missing_assets: [] }],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 2 }] }],
    ]);

    const user = userEvent.setup();
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("編集中のストーリー"), "story_1");

    const target = await screen.findByLabelText("Script の対象シーン");
    await user.selectOptions(target, "scene_02");
    await user.click(screen.getByRole("button", { name: "Script" }));

    await waitFor(() => {
      expect(calls.some((call) => call.url.includes("/expand"))).toBe(true);
    });
    expect(calls.find((call) => call.url.includes("/expand"))?.body).toMatchObject({
      task: "script",
      params: { scene_id: "scene_02" },
    });
  });

  it("shows the dialogue a script stage produced", async () => {
    const story = makeStory({
      logline: "l",
      beats: [{ id: "beat_01", act: "1", purpose: "p", summary: "s", order: 0 }],
      scenes: [
        makeScene({
          id: "scene_01",
          order: 0,
          dialogue: [{ speaker: "ミナ", text: "こっちへ", direction: "小声で" }],
        }),
      ],
    });
    stubFetch([
      ["/stories/story_1", { story, missing_assets: [] }],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 1 }] }],
    ]);

    const user = userEvent.setup();
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("編集中のストーリー"), "story_1");

    const table = await screen.findByRole("table");
    expect(within(table).getByText("台詞 1 行")).toBeTruthy();
    expect(within(table).getByText("こっちへ")).toBeTruthy();
  });

  it("calls the script stage complete only once a scene carries dialogue", async () => {
    // Narration is written by the scene_list stage, so a narration-only story has
    // not run Script yet — claiming otherwise hides the stage that #106 is about.
    const scene = makeScene({
      id: "scene_01",
      order: 0,
      narration: "朝の光が街を照らしていた。",
    });
    const withoutDialogue = makeStory({
      logline: "l",
      beats: [{ id: "beat_01", act: "1", purpose: "p", summary: "s", order: 0 }],
      scenes: [scene],
    });
    stubFetch([
      ["/stories/story_1", { story: withoutDialogue, missing_assets: [] }],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 1 }] }],
    ]);

    const user = userEvent.setup();
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("編集中のストーリー"), "story_1");

    const hint = await screen.findByText(/台詞を書く —/);
    expect(hint.textContent).not.toContain("完了");
    // The state is readable without relying on colour, and names the target.
    expect(hint.textContent).toContain("屋上の朝");

    cleanup();
    stubFetch([
      [
        "/stories/story_1",
        {
          story: makeStory({
            ...withoutDialogue,
            scenes: [
              { ...scene, dialogue: [{ speaker: "ミナ", text: "こっちへ" }] },
            ],
          }),
          missing_assets: [],
        },
      ],
      ["/stories", { items: [{ id: "story_1", title: "Rewind", scene_count: 1 }] }],
    ]);
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);
    await waitFor(() => {
      expect(screen.getByRole("option", { name: /Rewind/ })).toBeTruthy();
    });
    await user.selectOptions(screen.getByLabelText("編集中のストーリー"), "story_1");
    expect((await screen.findByText(/台詞を書く —/)).textContent).toContain(
      "完了・再生成可能",
    );
  });

  it("reports an API failure without losing the panel", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "boom" }), { status: 500 })),
    );
    render(<StoryPanel modelId="" awaitJob={async () => "succeeded"} />);
    expect((await screen.findByRole("alert")).textContent).toContain("boom");
  });
});

describe("MatrixPanel", () => {
  const templates = [
    {
      name: "logo-30",
      description: "30 logo construction patterns",
      first_stage_items: 30,
      stages: [
        { name: "probe", keep_top_n: 6 },
        { name: "refine", keep_top_n: null },
      ],
    },
  ];

  it("states the planned item count before anything is generated", async () => {
    stubFetch([["/batches/templates", templates]]);
    render(<MatrixPanel modelId="" />);

    expect(await screen.findByRole("button", { name: "30 件を生成" })).toBeTruthy();
    expect(screen.getByText(/1 段階目は 30 件/)).toBeTruthy();
    expect(screen.getByText(/比較グリッドが表示されます/)).toBeTruthy();
  });

  it("requires a prompt before submitting", async () => {
    stubFetch([["/batches/templates", templates]]);
    const user = userEvent.setup();
    render(<MatrixPanel modelId="" />);

    const submit = await screen.findByRole("button", { name: "30 件を生成" });
    expect(submit.hasAttribute("disabled")).toBe(true);

    await user.type(screen.getByLabelText("お題"), "acme logo");
    expect(submit.hasAttribute("disabled")).toBe(false);
  });

  it("sends the template with the prompt and renders the returned grid", async () => {
    const calls = stubFetch([
      ["/batches/templates", templates],
      ["/batches", makeBatch(), "POST"],
    ]);
    const user = userEvent.setup();
    render(<MatrixPanel modelId="sdxl" pollIntervalMs={100000} />);

    await user.type(await screen.findByLabelText("お題"), "acme logo");
    await user.click(screen.getByRole("button", { name: "30 件を生成" }));

    await waitFor(() => {
      expect(screen.getByText(/probe: 1\/2 完了/)).toBeTruthy();
    });

    const post = calls.find((call) => call.method === "POST");
    expect(post?.body).toEqual({
      template: "logo-30",
      overrides: { prompt: "acme logo", model_id: "sdxl" },
    });

    expect(screen.getByAltText("centered-wordmark")).toBeTruthy();
    expect(screen.getByText("最高スコア")).toBeTruthy();
    expect(screen.getByRole("button", { name: "中断する" })).toBeTruthy();
  });

  it("does not offer promote for an unfinished item", async () => {
    stubFetch([
      ["/batches/templates", templates],
      [
        "/batches",
        makeBatch({ items: [makeItem({ status: "running", score: null })] }),
        "POST",
      ],
    ]);
    const user = userEvent.setup();
    render(<MatrixPanel modelId="" pollIntervalMs={100000} />);

    await user.type(await screen.findByLabelText("お題"), "acme logo");
    await user.click(screen.getByRole("button", { name: "30 件を生成" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "採用" }).hasAttribute("disabled")).toBe(
        true,
      );
    });
    expect(screen.getByText("未採点")).toBeTruthy();
  });

  it("shows a failed item's error instead of a broken thumbnail", async () => {
    stubFetch([
      ["/batches/templates", templates],
      [
        "/batches",
        makeBatch({
          status: "partial",
          items: [
            makeItem({
              status: "failed",
              score: null,
              preview_path: null,
              output_path: null,
              error_message: "out of memory",
            }),
          ],
        }),
        "POST",
      ],
    ]);
    const user = userEvent.setup();
    render(<MatrixPanel modelId="" pollIntervalMs={100000} />);

    await user.type(await screen.findByLabelText("お題"), "acme logo");
    await user.click(screen.getByRole("button", { name: "30 件を生成" }));

    await waitFor(() => {
      expect(screen.getByText("out of memory")).toBeTruthy();
    });
    expect(screen.queryByAltText("centered-wordmark")).toBeNull();
    expect(screen.getAllByText("失敗").length).toBeGreaterThan(0);
    // A terminal batch offers no cancel action.
    expect(screen.queryByRole("button", { name: "中断する" })).toBeNull();
  });

  it("surfaces a template load failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "nope" }), { status: 500 })),
    );
    render(<MatrixPanel modelId="" />);
    expect((await screen.findByRole("alert")).textContent).toContain("nope");
  });
});

function makeScene(overrides: Partial<StoryScene> = {}): StoryScene {
  return {
    id: "scene_01",
    order: 0,
    heading: "屋上の朝",
    summary: "",
    narration: "",
    image_prompt: "rooftop at dawn",
    image_negative: "",
    bgm_mood: "hopeful",
    duration_seconds: 4,
    camera: "",
    asset_ids: {},
    ...overrides,
  } as StoryScene;
}

describe("scene generation helpers", () => {
  it("lists a scene's missing roles in production order", () => {
    const detail = {
      story: makeStory(),
      missing_assets: [
        { scene_id: "scene_01", role: "music" },
        { scene_id: "scene_01", role: "visual" },
        { scene_id: "scene_02", role: "visual" },
      ],
    } as never;
    expect(missingRolesForScene(detail, "scene_01")).toEqual(["visual", "music"]);
    expect(missingRolesForScene(detail, "scene_03")).toEqual([]);
    expect(missingRolesForScene(null, "scene_01")).toEqual([]);
  });

  it("is ready to assemble only when scenes exist and nothing is missing", () => {
    expect(isReadyToAssemble(null)).toBe(false);
    // Scenes present but incomplete.
    expect(
      isReadyToAssemble({
        story: makeStory({ scenes: [makeScene()] }),
        missing_assets: [{ scene_id: "scene_01", role: "visual" }],
      } as never),
    ).toBe(false);
    // No scenes at all is not "ready", it is empty.
    expect(
      isReadyToAssemble({ story: makeStory(), missing_assets: [] } as never),
    ).toBe(false);
    expect(
      isReadyToAssemble({
        story: makeStory({ scenes: [makeScene()] }),
        missing_assets: [],
      } as never),
    ).toBe(true);
  });
});
