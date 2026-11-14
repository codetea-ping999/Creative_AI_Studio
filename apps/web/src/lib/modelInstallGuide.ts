import type { MediaType, ModelInstallGuide, ModelOption } from "../components/promptFormTypes";

/** Where to fetch a missing model and which folder it belongs in. */
export function getInstallGuide(
  mediaType: MediaType,
  modelOption: ModelOption,
): ModelInstallGuide | null {
  const normalizedId = modelOption.id.toLowerCase();

  if (mediaType === "image") {
    if (normalizedId.includes("anime-sdxl")) {
      return {
        label: "Anime SDXL checkpoint",
        url: "https://civitai.com/search/models?baseModel=SDXL",
        note: "SDXL 系の anime checkpoint を取得して `models/image/anime-sdxl` に配置します。",
      };
    }
    if (normalizedId.includes("sdxl")) {
      return {
        label: "Stable Diffusion XL Base 1.0",
        url: "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0",
        note: "base model を取得して `models/image/sdxl` に配置します。",
      };
    }
  }

  if (mediaType === "audio") {
    if (normalizedId.includes("musicgen-melody")) {
      return {
        label: "MusicGen Melody",
        url: "https://huggingface.co/facebook/musicgen-melody",
        note: "checkpoint を取得して `models/audio/musicgen-melody` に配置します。",
      };
    }
    if (normalizedId.includes("musicgen-large")) {
      return {
        label: "MusicGen Large",
        url: "https://huggingface.co/facebook/musicgen-large",
        note: "checkpoint を取得して `models/audio/musicgen-large` に配置します。",
      };
    }
    if (normalizedId.includes("musicgen-medium")) {
      return {
        label: "MusicGen Medium",
        url: "https://huggingface.co/facebook/musicgen-medium",
        note: "checkpoint を取得して `models/audio/musicgen-medium` に配置します。",
      };
    }
    if (normalizedId.includes("musicgen-small")) {
      return {
        label: "MusicGen Small",
        url: "https://huggingface.co/facebook/musicgen-small",
        note: "checkpoint を取得して `models/audio/musicgen-small` に配置します。",
      };
    }
  }

  if (mediaType === "video" && normalizedId.includes("learned-video")) {
    return {
      label: "CogVideoX-2B",
      url: "https://huggingface.co/THUDM/CogVideoX-2b",
      note: "Diffusers形式のweightを `models/video/cogvideox-2b` に配置します。",
    };
  }

  return null;
}
