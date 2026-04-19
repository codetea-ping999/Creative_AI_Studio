import type { MediaType, ModelOption } from "./promptFormTypes";

export type ImagePreset = {
  name: string;
  prompt: string;
  negativePrompt: string;
};

export type AudioPreset = {
  name: string;
  prompt: string;
  mood: string;
  bpm: number;
  durationSeconds: number;
};

export type VideoPreset = {
  name: string;
  prompt: string;
  negativePrompt: string;
  width: number;
  height: number;
  durationSeconds: number;
  cameraMotion: string;
  visualStyle: string;
};

export type ModelInstallGuide = {
  label: string;
  url: string;
  note: string;
};

export const imagePresets: ImagePreset[] = [
  {
    name: "アニメ人物",
    prompt:
      "anime style, Sakurajima Mai, solo, long straight black hair, purple eyes, blunt bangs, small bunny hair clip, beige cardigan, white shirt, red necktie, grey pleated skirt, black pantyhose, school hallway, soft window light, detailed anime face",
    negativePrompt:
      "bad anatomy, extra fingers, red eyes, blue hair, missing hair clip, text, watermark, blurry, low quality",
  },
  {
    name: "キービジュアル",
    prompt:
      "cinematic key visual, futuristic creative studio, mixed reality control room, glowing screens, large window light, polished hardware, editorial composition, dramatic contrast, highly detailed",
    negativePrompt:
      "flat lighting, bad hands, extra limbs, messy composition, low detail, text, watermark",
  },
];

export const audioPresets: AudioPreset[] = [
  {
    name: "ドリーミーループ",
    prompt: "dreamy ambient synth loop, soft arp, night city glow, floating texture",
    mood: "dreamy",
    bpm: 92,
    durationSeconds: 8,
  },
  {
    name: "パルスドライブ",
    prompt: "energetic electronic loop, punchy bass, bright lead, creative momentum",
    mood: "energetic",
    bpm: 122,
    durationSeconds: 10,
  },
];

export const videoPresets: VideoPreset[] = [
  {
    name: "ムードリール",
    prompt:
      "cinematic storyboard, night drive through neon city, reflective asphalt, elevated camera, bold contrast",
    negativePrompt: "low motion, flat composition, cluttered frame",
    width: 576,
    height: 320,
    durationSeconds: 4,
    cameraMotion: "push-in",
    visualStyle: "storyboard",
  },
  {
    name: "ブランド導入",
    prompt:
      "editorial motion board, premium product reveal, clean geometry, strong center framing, warm accent glow",
    negativePrompt: "chaotic movement, muddy lighting, text overlays",
    width: 640,
    height: 360,
    durationSeconds: 5,
    cameraMotion: "orbit",
    visualStyle: "editorial-board",
  },
];

export const imageFormatPresets = [
  { value: "square", label: "正方形 1:1", width: 1024, height: 1024 },
  { value: "portrait", label: "縦長 4:5", width: 832, height: 1024 },
  { value: "landscape", label: "横長 4:3", width: 1024, height: 768 },
  { value: "wide", label: "ワイド 16:9", width: 1344, height: 768 },
] as const;

export function resolveImageFormatPreset(width: number, height: number): string {
  const matchedPreset = imageFormatPresets.find(
    (preset) => preset.width === width && preset.height === height,
  );
  return matchedPreset?.value ?? "custom";
}

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
        note: "SDXL 系のアニメ向け checkpoint を取得して `models/image/anime-sdxl` に配置します。",
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

  return null;
}
