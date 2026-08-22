import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { excerptFromMarkdown, useTextAssetContent } from "./textAssetPreview";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("excerptFromMarkdown", () => {
  it("strips headers, list markers, and emphasis into plain text", () => {
    const markdown = [
      "# Loglines",
      "",
      "1. A heist crew must pull one last job before dawn.",
      "   - hook: **the vault** never opens twice",
      "   - tone: *tense*",
      "",
    ].join("\n");

    expect(excerptFromMarkdown(markdown)).toBe(
      "Loglines A heist crew must pull one last job before dawn. hook: the vault never opens twice tone: tense",
    );
  });

  it("truncates long content to the requested character budget with an ellipsis", () => {
    const long = "word ".repeat(80).trim();
    const excerpt = excerptFromMarkdown(long, 40);

    expect(excerpt.length).toBeLessThanOrEqual(40);
    expect(excerpt.endsWith("…")).toBe(true);
  });

  it("returns short content unchanged", () => {
    expect(excerptFromMarkdown("- a short bullet")).toBe("a short bullet");
  });
});

describe("useTextAssetContent", () => {
  it("returns null content and is not loading when src is null", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useTextAssetContent(null));

    expect(result.current.content).toBeNull();
    expect(result.current.isLoading).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("starts loading, then resolves fetched content", async () => {
    const src = "/outputs/test-load.md";
    let resolveText: (value: string) => void = () => {};
    const textPromise = new Promise<string>((resolve) => {
      resolveText = resolve;
    });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: () => textPromise });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useTextAssetContent(src));

    expect(result.current.isLoading).toBe(true);
    expect(result.current.content).toBeNull();

    resolveText("loaded content");
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.content).toBe("loaded content");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(src);
  });

  it("returns cached content synchronously on remount without calling fetch again", async () => {
    const src = "/outputs/test-cache.md";
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve("cached content") });
    vi.stubGlobal("fetch", fetchMock);

    const first = renderHook(() => useTextAssetContent(src));
    await waitFor(() => expect(first.result.current.isLoading).toBe(false));
    expect(first.result.current.content).toBe("cached content");
    first.unmount();

    const second = renderHook(() => useTextAssetContent(src));

    expect(second.result.current.content).toBe("cached content");
    expect(second.result.current.isLoading).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("dedupes concurrent requests for the same src into a single fetch call", async () => {
    const src = "/outputs/test-dedupe.md";
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve("deduped content") });
    vi.stubGlobal("fetch", fetchMock);

    const a = renderHook(() => useTextAssetContent(src));
    const b = renderHook(() => useTextAssetContent(src));

    expect(a.result.current.isLoading).toBe(true);
    expect(b.result.current.isLoading).toBe(true);

    await waitFor(() => expect(a.result.current.isLoading).toBe(false));
    await waitFor(() => expect(b.result.current.isLoading).toBe(false));

    expect(a.result.current.content).toBe("deduped content");
    expect(b.result.current.content).toBe("deduped content");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sets content to null when the response is not ok", async () => {
    const src = "/outputs/test-http-error.md";
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404, text: () => Promise.resolve("") });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useTextAssetContent(src));

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.content).toBeNull();
  });

  it("does not warn about updating state on an unmounted component when the fetch resolves late", async () => {
    const src = "/outputs/test-unmount.md";
    let resolveText: (value: string) => void = () => {};
    const textPromise = new Promise<string>((resolve) => {
      resolveText = resolve;
    });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: () => textPromise });
    vi.stubGlobal("fetch", fetchMock);
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = renderHook(() => useTextAssetContent(src));
    unmount();

    await act(async () => {
      resolveText("late content");
      await textPromise;
    });

    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });
});
