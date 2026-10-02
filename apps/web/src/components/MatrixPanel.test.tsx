import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Batch, BatchItem } from "../lib/batchApi";
import { MatrixPanel } from "./MatrixPanel";

const template = { name: "logo", description: "Logo variants", first_stage_items: 2 };

function makeItem(overrides: Partial<BatchItem> = {}): BatchItem {
  return {
    id: "first",
    index: 0,
    label: "First",
    stage_name: "probe",
    stage_index: 0,
    axis_values: {},
    job_id: "job-first",
    status: "succeeded",
    score: 80,
    output_path: "outputs/first.gif",
    preview_path: null,
    error_message: null,
    promoted: false,
    ...overrides,
  };
}

function makeBatch(items: BatchItem[]): Batch {
  return {
    id: "batch-1",
    name: "GIF comparison",
    media_type: "image",
    project_id: null,
    status: "succeeded",
    stage_index: 0,
    stage_names: ["probe"],
    aggregate: {
      total: items.length,
      pending: 0,
      running: 0,
      succeeded: items.length,
      failed: 0,
      cancelled: 0,
      average_score: 80,
      best_item_id: items[0]?.id ?? null,
    },
    items,
    error_message: null,
    created_at: "2026-07-26T00:00:00+00:00",
    updated_at: "2026-07-26T00:00:00+00:00",
  };
}

function stubBatch(items: BatchItem[]) {
  const batch = makeBatch(items);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = url.includes("/batches/templates")
      ? [template]
      : init?.method === "POST" || url.includes("/batches/batch-1")
        ? batch
        : { items: [] };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
}

function stubMotion(initial: boolean) {
  const listeners = new Set<() => void>();
  const query = {
    matches: initial,
    media: "(prefers-reduced-motion: reduce)",
    addEventListener: vi.fn((_type: string, listener: () => void) => listeners.add(listener)),
    removeEventListener: vi.fn((_type: string, listener: () => void) => listeners.delete(listener)),
  };
  vi.stubGlobal("matchMedia", vi.fn(() => query));
  return (matches: boolean) => {
    act(() => {
      query.matches = matches;
      listeners.forEach((listener) => listener());
    });
  };
}

class FakeImage {
  static instances: FakeImage[] = [];
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = 4;
  naturalHeight = 3;
  src = "";
  constructor() {
    FakeImage.instances.push(this);
  }
}

async function showBatch(items: BatchItem[]) {
  stubBatch(items);
  const user = userEvent.setup();
  const view = render(<MatrixPanel modelId="" />);
  await user.type(await screen.findByLabelText("お題"), "logo");
  await user.click(screen.getByRole("button", { name: "2 件を生成" }));
  await screen.findByRole("button", { name: `${items[0].label}を拡大表示` });
  return { user, ...view };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  FakeImage.instances = [];
});

describe("MatrixPanel GIF previews", () => {
  it("shows static canvases in both surfaces under reduced motion, with no playback control", async () => {
    stubMotion(true);
    const { user, container } = await showBatch([makeItem()]);
    const thumb = screen.getByRole("button", { name: "Firstを拡大表示" });
    expect(within(thumb).getByRole("img", { name: "First" }).tagName).toBe("CANVAS");
    expect(container.querySelector(".matrix-cell__thumb img")).toBeNull();

    await user.click(thumb);
    expect(screen.getByRole("img", { name: "First の拡大プレビュー" }).tagName).toBe("CANVAS");
    expect(container.querySelector(".matrix-inspector__media img")).toBeNull();
    expect(screen.queryByRole("button", { name: /Play animation|Show still frame/ })).toBeNull();
  });

  it("keeps GIF animation and PNG previews as images at normal motion preference", async () => {
    stubMotion(false);
    const { user, container } = await showBatch([
      makeItem(),
      makeItem({ id: "png", index: 1, label: "PNG", output_path: "outputs/still.png" }),
    ]);
    expect(screen.getByRole("img", { name: "First" }).tagName).toBe("IMG");
    await user.click(screen.getByRole("button", { name: "Firstを拡大表示" }));
    expect(screen.getByRole("img", { name: "First の拡大プレビュー" }).tagName).toBe("IMG");
    await user.click(screen.getByRole("button", { name: "PNGを拡大表示" }));
    expect(screen.getByRole("img", { name: "PNG" }).tagName).toBe("IMG");
    expect(screen.getByRole("img", { name: "PNG の拡大プレビュー" }).tagName).toBe("IMG");
    expect(container.querySelector("canvas")).toBeNull();
  });

  it("uses preview_path ahead of output_path when choosing image or GIF", async () => {
    stubMotion(true);
    const { user } = await showBatch([
      makeItem({ preview_path: "outputs/still.png", output_path: "outputs/animated.gif" }),
      makeItem({
        id: "second",
        index: 1,
        label: "Second",
        preview_path: "outputs/PREVIEW.GIF?token=abc#frame",
        output_path: "outputs/still.png",
      }),
    ]);
    expect(screen.getByRole("img", { name: "First" }).tagName).toBe("IMG");
    await user.click(screen.getByRole("button", { name: "Firstを拡大表示" }));
    expect(screen.getByRole("img", { name: "First の拡大プレビュー" }).tagName).toBe("IMG");
    expect(screen.getByRole("img", { name: "First" }).getAttribute("src")).toMatch(/still\.png$/);

    await user.click(screen.getByRole("button", { name: "Secondを拡大表示" }));
    expect(screen.getByRole("img", { name: "Second" }).tagName).toBe("CANVAS");
    expect(screen.getByRole("img", { name: "Second の拡大プレビュー" }).tagName).toBe("CANVAS");
  });

  it("updates both surfaces when the motion preference changes live", async () => {
    const setMotion = stubMotion(false);
    const { user } = await showBatch([makeItem()]);
    await user.click(screen.getByRole("button", { name: "Firstを拡大表示" }));
    expect(screen.getByRole("img", { name: "First" }).tagName).toBe("IMG");
    expect(screen.getByRole("img", { name: "First の拡大プレビュー" }).tagName).toBe("IMG");
    setMotion(true);
    expect(screen.getByRole("img", { name: "First" }).tagName).toBe("CANVAS");
    expect(screen.getByRole("img", { name: "First の拡大プレビュー" }).tagName).toBe("CANVAS");
    setMotion(false);
    expect(screen.getByRole("img", { name: "First" }).tagName).toBe("IMG");
    expect(screen.getByRole("img", { name: "First の拡大プレビュー" }).tagName).toBe("IMG");
  });

  it("clears a failed still-frame load when selecting another GIF", async () => {
    stubMotion(true);
    vi.stubGlobal("Image", FakeImage);
    vi.stubGlobal("IntersectionObserver", undefined);
    const { user } = await showBatch([
      makeItem(),
      makeItem({ id: "second", index: 1, label: "Second", output_path: "outputs/second.gif" }),
    ]);
    await user.click(screen.getByRole("button", { name: "Firstを拡大表示" }));
    await waitFor(() => expect(FakeImage.instances.length).toBeGreaterThanOrEqual(3));
    const firstInspectorImage = [...FakeImage.instances]
      .reverse()
      .find((image) => image.src.endsWith("first.gif") && image.onerror);
    expect(firstInspectorImage).toBeTruthy();
    act(() => firstInspectorImage?.onerror?.());
    expect(within(screen.getByRole("region", { name: "First" })).getByText("No preview")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Secondを拡大表示" }));
    expect(within(screen.getByRole("region", { name: "Second" })).queryByText("No preview")).toBeNull();
    expect(screen.getByRole("img", { name: "Second の拡大プレビュー" }).tagName).toBe("CANVAS");
    expect(within(screen.getByRole("button", { name: "Secondを拡大表示" })).getByRole("img", { name: "Second" }).tagName).toBe("CANVAS");
  });
});
