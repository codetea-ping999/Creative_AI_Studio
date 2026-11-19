import { describe, expect, it } from "vitest";
import {
  applyPresetPrompt,
  assertValidImagePresets,
  productionImagePresets,
  validateImagePreset,
  type ImagePresetSpec,
} from "./imagePresets";

const manifestModules = import.meta.glob<{ public_id: string; enabled?: boolean }>(
  "../../../../models/manifests/image/*.json",
  { eager: true, import: "default" },
);
const imageModelIds = Object.values(manifestModules).map((manifest) => manifest.public_id);

const minimal: ImagePresetSpec = {
  id: "fixture",
  name: "テスト",
  intent: "Fixture preset.",
  modelId: "sdxl",
  promptAdditions: "test style",
  negativePrompt: "",
  width: 1024,
  height: 1024,
  steps: 30,
  guidanceScale: 7,
};

describe("image preset contract", () => {
  it("accepts a minimal valid preset", () => {
    expect(validateImagePreset(minimal, ["sdxl"])).toEqual([]);
  });

  it("reports actionable errors for unsupported values", () => {
    const errors = validateImagePreset(
      {
        ...minimal,
        id: "Bad Id",
        modelId: "missing-model",
        width: 1000,
        height: 4096,
        steps: 0,
        guidanceScale: 99,
        loraScale: 3,
      },
      ["sdxl"],
    );
    expect(errors.join("\n")).toMatch(/kebab-case/);
    expect(errors.join("\n")).toMatch(/"missing-model" has no image manifest; use one of sdxl/);
    expect(errors.join("\n")).toMatch(/width must be a multiple of 64/);
    expect(errors.join("\n")).toMatch(/height must be a multiple of 64/);
    expect(errors.join("\n")).toMatch(/steps must be an integer/);
    expect(errors.join("\n")).toMatch(/guidanceScale must be between/);
    expect(errors.join("\n")).toMatch(/loraScale must be between/);
  });

  it("throws on duplicate ids instead of silently picking one", () => {
    expect(() => assertValidImagePresets([minimal, minimal])).toThrow(/duplicate preset id/);
  });

  it("ships Portrait, Anime and Cinematic, each pointing at an image manifest", () => {
    expect(imageModelIds.length).toBeGreaterThan(0);
    expect(productionImagePresets.map((preset) => preset.id)).toEqual([
      "portrait",
      "anime",
      "cinematic",
    ]);
    expect(() => assertValidImagePresets(productionImagePresets, imageModelIds)).not.toThrow();
  });
});

describe("applyPresetPrompt", () => {
  const [portrait, anime] = productionImagePresets;

  it("appends style words to the subject", () => {
    expect(applyPresetPrompt("a violinist", portrait)).toBe(
      `a violinist, ${portrait.promptAdditions}`,
    );
  });

  it("uses only the style words when there is no subject", () => {
    expect(applyPresetPrompt("  ", anime)).toBe(anime.promptAdditions);
  });

  it("replaces another preset's style words instead of stacking them", () => {
    const withPortrait = applyPresetPrompt("a violinist", portrait);
    expect(applyPresetPrompt(withPortrait, anime)).toBe(`a violinist, ${anime.promptAdditions}`);
    expect(applyPresetPrompt(applyPresetPrompt("", portrait), anime)).toBe(anime.promptAdditions);
  });
});
