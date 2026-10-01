import { afterEach, describe, expect, it, vi } from "vitest";

import { requestJson } from "./studioClient";

describe("requestJson", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

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

    await expect(requestJson("/generate/image")).rejects.toThrow(
      "body.params.width: must be positive",
    );
  });

  it("keeps a non-JSON API failure readable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("runtime unavailable", { status: 503 })),
    );

    await expect(requestJson("/generate/video")).rejects.toThrow("runtime unavailable");
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
