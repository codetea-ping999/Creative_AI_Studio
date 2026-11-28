import { describe, expect, it } from "vitest";
import type { ModelOption } from "../components/promptFormTypes";
import { getMediaReadiness, pickStartingMedia } from "./mediaReadiness";

function model(isAvailable: boolean): ModelOption {
  return {
    id: isAvailable ? "ready-model" : "missing-model",
    displayName: "Model",
    defaultParams: {},
    tags: [],
    isAvailable,
    isDefault: true,
    runtimeStatus: isAvailable ? "ready" : "missing_files",
    availabilityMessage: "",
    installPath: "",
    supportsLocalInstall: false,
  };
}

describe("getMediaReadiness", () => {
  it("is ready only once a loaded list holds an available model", () => {
    expect(getMediaReadiness("loading", [model(true)])).toBe("loading");
    expect(getMediaReadiness("loaded", [model(false), model(true)])).toBe("ready");
    expect(getMediaReadiness("loaded", [model(false)])).toBe("missing");
    expect(getMediaReadiness("loaded", [])).toBe("missing");
    expect(getMediaReadiness("error", [model(true)])).toBe("error");
  });
});

describe("pickStartingMedia", () => {
  it("keeps a media type that already works", () => {
    expect(
      pickStartingMedia("audio", { image: "ready", audio: "ready", video: "ready" }),
    ).toBe("audio");
  });

  it("waits until every media type has answered", () => {
    expect(
      pickStartingMedia("image", { image: "missing", audio: "loading", video: "ready" }),
    ).toBeNull();
  });

  it("moves off a media type that cannot generate to the first that can", () => {
    expect(
      pickStartingMedia("image", { image: "missing", audio: "missing", video: "ready" }),
    ).toBe("video");
  });

  it("stays put when nothing can generate", () => {
    expect(
      pickStartingMedia("image", { image: "missing", audio: "error", video: "missing" }),
    ).toBe("image");
  });
});
