import type {
  AudioPreset,
  ImagePreset,
  PromptFormSubmitValues,
  VideoPreset,
} from "./promptFormTypes";

export const defaultPromptFormValues: PromptFormSubmitValues = {
  mediaType: "image",
  modelId: "sdxl",
  outputFormat: "png",
  prompt: "",
  negativePrompt: "",
  imageBriefPurpose: "SNS投稿",
  imageBriefSubject: "",
  imageBriefMood: "やわらかい光",
  width: 1024,
  height: 1024,
  steps: 30,
  guidanceScale: 7.5,
  loraPath: "",
  loraScale: 0.8,
  seed: null,
  durationSeconds: 8,
  bpm: 96,
  mood: "dreamy",
  genre: "electronic",
  instruments: "warm analog synth, soft percussion",
  structure: "seamless loop",
  temperature: 1,
  topK: 250,
  topP: 0,
  cameraMotion: "push-in",
  visualStyle: "storyboard",
};

export const imagePresets: ImagePreset[] = [
  {
    name: "Anime Portrait",
    prompt:
      "anime style, Sakurajima Mai, solo, long straight black hair, purple eyes, blunt bangs, small bunny hair clip, beige cardigan, white shirt, red necktie, grey pleated skirt, black pantyhose, school hallway, soft window light, detailed anime face",
    negativePrompt:
      "bad anatomy, extra fingers, red eyes, blue hair, missing hair clip, text, watermark, blurry, low quality",
  },
  {
    name: "Key Visual",
    prompt:
      "cinematic key visual, futuristic creative studio, mixed reality control room, glowing screens, large window light, polished hardware, editorial composition, dramatic contrast, highly detailed",
    negativePrompt:
      "flat lighting, bad hands, extra limbs, messy composition, low detail, text, watermark",
  },
];

export const audioPresets: AudioPreset[] = [
  {
    name: "Dreamy Loop",
    prompt: "dreamy ambient synth loop, soft arp, night city glow, floating texture",
    mood: "dreamy",
    genre: "ambient",
    instruments: "soft analog synth, airy pads, gentle arpeggio",
    structure: "seamless loop",
    bpm: 92,
    durationSeconds: 8,
  },
  {
    name: "Pulse Driver",
    prompt: "energetic electronic loop, punchy bass, bright lead, creative momentum",
    mood: "energetic",
    genre: "electronic",
    instruments: "punchy bass, bright synth lead, electronic drums",
    structure: "intro, build, drop",
    bpm: 122,
    durationSeconds: 10,
  },
  {
    name: "Cinematic Cue",
    prompt: "cinematic tension cue, restrained opening, emotional rise, decisive ending",
    mood: "dark",
    genre: "cinematic",
    instruments: "low strings, piano, deep percussion, brass swells",
    structure: "intro, development, climax",
    bpm: 84,
    durationSeconds: 12,
  },
];

export const videoPresets: VideoPreset[] = [
  {
    name: "Mood Reel",
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
    name: "Brand Opener",
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
  { value: "square", label: "Square 1:1", width: 1024, height: 1024 },
  { value: "portrait", label: "Portrait 4:5", width: 832, height: 1024 },
  { value: "landscape", label: "Landscape 4:3", width: 1024, height: 768 },
  { value: "wide", label: "Wide 16:9", width: 1344, height: 768 },
] as const;
