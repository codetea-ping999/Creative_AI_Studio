import type { MediaType, ModelOption } from "../components/promptFormTypes";

export type ModelLoadState = "idle" | "loading" | "loaded" | "error";

/** Whether a media type can generate on this machine right now. */
export type MediaReadiness = "loading" | "ready" | "missing" | "error";

export const mediaKindLabels: Record<MediaType, string> = {
  image: "画像",
  audio: "音声",
  video: "動画",
};

/** Order tried when the studio picks a starting media type by itself. */
export const startingMediaOrder: MediaType[] = ["image", "video", "audio"];

export function getMediaReadiness(
  loadState: ModelLoadState,
  models: ModelOption[],
): MediaReadiness {
  if (loadState === "error") {
    return "error";
  }
  if (loadState !== "loaded") {
    return "loading";
  }
  return models.some((model) => model.isAvailable) ? "ready" : "missing";
}

/**
 * Picks the media type to open on when nobody chose one yet: keep the current
 * one if it works, otherwise the first that does. Returns null while any type
 * is still loading so the screen does not jump twice.
 */
export function pickStartingMedia(
  current: MediaType,
  readiness: Record<MediaType, MediaReadiness>,
): MediaType | null {
  if (readiness[current] === "ready") {
    return current;
  }
  if (startingMediaOrder.some((media) => readiness[media] === "loading")) {
    return null;
  }
  return startingMediaOrder.find((media) => readiness[media] === "ready") ?? current;
}
