import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type StudioClientModule = typeof import("./studioClient");

describe("requestJson", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  beforeEach(() => {
    // `getBackendBaseUrl()` caches its resolution at module scope; resetting
    // modules between tests keeps each runtime strategy isolated.
    vi.resetModules();
  });

  async function loadClient(): Promise<StudioClientModule> {
    return import("./studioClient");
  }

  it("formats FastAPI validation errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [{ loc: ["body", "params", "width"], msg: "must be positive" }],
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const { requestJson } = await loadClient();
    await expect(requestJson("/generate/image")).rejects.toThrow(
      "body.params.width: must be positive",
    );
  });

  it("keeps a non-JSON API failure readable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("runtime unavailable", { status: 503 })),
    );

    const { requestJson } = await loadClient();
    await expect(requestJson("/generate/video")).rejects.toThrow("runtime unavailable");
  });

  it("uses the base URL resolved from the active runtime", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    window.__TAURI_INTERNALS__ = {
      invoke: vi.fn().mockResolvedValue("http://127.0.0.1:8123"),
    };

    const { requestJson } = await loadClient();
    await requestJson("/health");

    expect(window.__TAURI_INTERNALS__?.invoke).toHaveBeenCalledWith("get_backend_endpoint");
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8123/health", undefined);
  });
});