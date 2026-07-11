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
});
