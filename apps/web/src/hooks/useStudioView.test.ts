import { describe, expect, it } from "vitest";
import { parseStudioViewHash, viewHref } from "./useStudioView";

describe("parseStudioViewHash", () => {
  it("reads each screen from the hash", () => {
    expect(parseStudioViewHash("#/story")).toBe("story");
    expect(parseStudioViewHash("#compare")).toBe("compare");
    expect(parseStudioViewHash("#/setup/models")).toBe("setup");
  });

  it("falls back to create for an empty or unknown hash", () => {
    expect(parseStudioViewHash("")).toBe("create");
    expect(parseStudioViewHash("#/")).toBe("create");
    expect(parseStudioViewHash("#/gallery")).toBe("create");
  });

  it("round-trips through viewHref", () => {
    expect(parseStudioViewHash(viewHref("compare"))).toBe("compare");
  });
});
