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

  it("fetches on the same origin when VITE_API_BASE_URL is empty", async () => {
    // Frozen v1.0 release contract: the artifact is built with
    // VITE_API_BASE_URL="" so the served UI calls the API on its own origin
    // and no port is baked into the release. The module top-level constant
    // reads the env at import time, so re-import after setting the env.
    const previous = import.meta.env.VITE_API_BASE_URL;
    import.meta.env.VITE_API_BASE_URL = "";
    try {
      vi.resetModules();
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response('{"status":"ok"}', { status: 200 })),
      );
      const freshClient = await import("./studioClient");
      await freshClient.requestJson("/health");
      expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/health");
    } finally {
      if (previous === undefined) {
        delete import.meta.env.VITE_API_BASE_URL;
      } else {
        import.meta.env.VITE_API_BASE_URL = previous;
      }
      vi.resetModules();
    }
  });
});

describe("requestJson structured errors", () => {
  it("surfaces the message and code of an object detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ detail: { code: "destination_not_empty", message: "files exist" } }),
          { status: 409 },
        ),
      ),
    );
    const { ApiError, requestJson } = await import("./studioClient");

    const failure = await requestJson("/models/x/install").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as InstanceType<typeof ApiError>).message).toBe("files exist");
    expect((failure as InstanceType<typeof ApiError>).code).toBe("destination_not_empty");
    expect((failure as InstanceType<typeof ApiError>).status).toBe(409);
    vi.unstubAllGlobals();
  });
});
