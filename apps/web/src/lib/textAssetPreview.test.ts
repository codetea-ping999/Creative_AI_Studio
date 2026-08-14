import { describe, expect, it } from "vitest";
import { excerptFromMarkdown } from "./textAssetPreview";

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
