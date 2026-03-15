import { useEffect, useState, type FormEvent } from "react";

export type MediaType = "image" | "audio" | "video";

export type ModelOption = {
  id: string;
  displayName: string;
  defaultParams: Record<string, unknown>;
  tags: string[];
  isAvailable: boolean;
  isDefault: boolean;
};

export type LoraOption = {
  id: string;
  displayName: string;
  path: string;
  relativePath: string;
};

type PromptFormState = {
  modelId: string;
  prompt: string;
  negativePrompt: string;
  width: string;
  height: string;
  steps: string;
  guidanceScale: string;
  loraPath: string;
  loraScale: string;
  seed: string;
  durationSeconds: string;
  bpm: string;
  mood: string;
  cameraMotion: string;
  visualStyle: string;
};

export type PromptFormSubmitValues = {
  mediaType: MediaType;
  modelId: string;
  prompt: string;
  negativePrompt: string;
  width: number;
  height: number;
  steps: number;
  guidanceScale: number;
  loraPath: string;
  loraScale: number;
  seed: number | null;
  durationSeconds: number;
  bpm: number;
  mood: string;
  cameraMotion: string;
  visualStyle: string;
};

export type PromptFormProps = {
  formId?: string;
  mediaType?: MediaType;
  modelOptions?: ModelOption[];
  loraOptions?: LoraOption[];
  initialValues?: Partial<PromptFormSubmitValues>;
  submitLabel?: string;
  disabled?: boolean;
  canSubmit?: boolean;
  statusMessage?: string | null;
  onSubmit?: (values: PromptFormSubmitValues) => void;
  onDraftChange?: (values: Partial<PromptFormSubmitValues>) => void;
};

type ImagePreset = {
  name: string;
  prompt: string;
  negativePrompt: string;
};

type AudioPreset = {
  name: string;
  prompt: string;
  mood: string;
  bpm: number;
  durationSeconds: number;
};

type VideoPreset = {
  name: string;
  prompt: string;
  negativePrompt: string;
  width: number;
  height: number;
  durationSeconds: number;
  cameraMotion: string;
  visualStyle: string;
};

type ControlMode = "quick" | "advanced";

type ModelInstallGuide = {
  label: string;
  url: string;
  note: string;
};

const defaultValues: PromptFormSubmitValues = {
  mediaType: "image",
  modelId: "sdxl",
  prompt: "",
  negativePrompt: "",
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
  cameraMotion: "push-in",
  visualStyle: "storyboard",
};

const imagePresets: ImagePreset[] = [
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

const audioPresets: AudioPreset[] = [
  {
    name: "Dreamy Loop",
    prompt: "dreamy ambient synth loop, soft arp, night city glow, floating texture",
    mood: "dreamy",
    bpm: 92,
    durationSeconds: 8,
  },
  {
    name: "Pulse Driver",
    prompt: "energetic electronic loop, punchy bass, bright lead, creative momentum",
    mood: "energetic",
    bpm: 122,
    durationSeconds: 10,
  },
];

const videoPresets: VideoPreset[] = [
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

const imageFormatPresets = [
  { value: "square", label: "Square 1:1", width: 1024, height: 1024 },
  { value: "portrait", label: "Portrait 4:5", width: 832, height: 1024 },
  { value: "landscape", label: "Landscape 4:3", width: 1024, height: 768 },
  { value: "wide", label: "Wide 16:9", width: 1344, height: 768 },
] as const;

function createInitialState(
  initialValues?: Partial<PromptFormSubmitValues>,
): PromptFormState {
  const merged = { ...defaultValues, ...initialValues };

  return {
    modelId: merged.modelId,
    prompt: merged.prompt,
    negativePrompt: merged.negativePrompt,
    width: String(merged.width),
    height: String(merged.height),
    steps: String(merged.steps),
    guidanceScale: String(merged.guidanceScale),
    loraPath: merged.loraPath,
    loraScale: String(merged.loraScale),
    seed: merged.seed === null ? "" : String(merged.seed),
    durationSeconds: String(merged.durationSeconds),
    bpm: String(merged.bpm),
    mood: merged.mood,
    cameraMotion: merged.cameraMotion,
    visualStyle: merged.visualStyle,
  };
}

function parseRequiredInteger(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parseRequiredFloat(value: string, fallback: number): number {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parseOptionalInteger(value: string): number | null {
  if (value.trim() === "") {
    return null;
  }

  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function serializeDraft(
  mediaType: MediaType,
  formValues: PromptFormState,
): Partial<PromptFormSubmitValues> {
  return {
    mediaType,
    modelId: formValues.modelId,
    prompt: formValues.prompt,
    negativePrompt: formValues.negativePrompt,
    width: parseRequiredInteger(formValues.width, defaultValues.width),
    height: parseRequiredInteger(formValues.height, defaultValues.height),
    steps: parseRequiredInteger(formValues.steps, defaultValues.steps),
    guidanceScale: parseRequiredFloat(
      formValues.guidanceScale,
      defaultValues.guidanceScale,
    ),
    loraPath: formValues.loraPath.trim(),
    loraScale: parseRequiredFloat(formValues.loraScale, defaultValues.loraScale),
    seed: parseOptionalInteger(formValues.seed),
    durationSeconds: parseRequiredInteger(
      formValues.durationSeconds,
      defaultValues.durationSeconds,
    ),
    bpm: parseRequiredInteger(formValues.bpm, defaultValues.bpm),
    mood: formValues.mood,
    cameraMotion: formValues.cameraMotion,
    visualStyle: formValues.visualStyle,
  };
}

function resolveImageFormatPreset(width: number, height: number): string {
  const matchedPreset = imageFormatPresets.find(
    (preset) => preset.width === width && preset.height === height,
  );
  return matchedPreset?.value ?? "custom";
}

function getInstallGuide(
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

  return null;
}

export function PromptForm({
  formId,
  mediaType = "image",
  modelOptions = [],
  loraOptions = [],
  initialValues,
  submitLabel = "Generate",
  disabled = false,
  canSubmit = true,
  statusMessage = null,
  onSubmit,
  onDraftChange,
}: PromptFormProps) {
  const [formValues, setFormValues] = useState<PromptFormState>(() =>
    createInitialState(initialValues),
  );
  const [controlMode, setControlMode] = useState<ControlMode>("quick");
  const availableModelCount = modelOptions.filter((option) => option.isAvailable).length;
  const unavailableModelCount = modelOptions.length - availableModelCount;
  const imageFormatValue = resolveImageFormatPreset(
    parseRequiredInteger(formValues.width, defaultValues.width),
    parseRequiredInteger(formValues.height, defaultValues.height),
  );
  const missingModelGuides = modelOptions
    .filter((option) => !option.isAvailable)
    .map((option) => ({
      modelId: option.id,
      displayName: option.displayName,
      guide: getInstallGuide(mediaType, option),
    }))
    .filter(
      (
        item,
      ): item is {
        modelId: string;
        displayName: string;
        guide: ModelInstallGuide;
      } => item.guide !== null,
    );

  useEffect(() => {
    onDraftChange?.(serializeDraft(mediaType, formValues));
  }, [formValues, mediaType, onDraftChange]);

  const setFieldValue = <K extends keyof PromptFormState>(
    field: K,
    value: PromptFormState[K],
  ) => {
    setFormValues((current) => ({
      ...current,
      [field]: value,
    }));
  };

  const applyImagePreset = (preset: ImagePreset) => {
    setFormValues((current) => ({
      ...current,
      prompt: preset.prompt,
      negativePrompt: preset.negativePrompt,
    }));
  };

  const applyAudioPreset = (preset: AudioPreset) => {
    setFormValues((current) => ({
      ...current,
      prompt: preset.prompt,
      mood: preset.mood,
      bpm: String(preset.bpm),
      durationSeconds: String(preset.durationSeconds),
    }));
  };

  const applyVideoPreset = (preset: VideoPreset) => {
    setFormValues((current) => ({
      ...current,
      prompt: preset.prompt,
      negativePrompt: preset.negativePrompt,
      width: String(preset.width),
      height: String(preset.height),
      durationSeconds: String(preset.durationSeconds),
      cameraMotion: preset.cameraMotion,
      visualStyle: preset.visualStyle,
    }));
  };

  const handleModelChange = (modelId: string) => {
    const selectedModel = modelOptions.find((option) => option.id === modelId);
    setFormValues((current) => ({
      ...current,
      modelId,
      width:
        typeof selectedModel?.defaultParams.width === "number"
          ? String(selectedModel.defaultParams.width)
          : current.width,
      height:
        typeof selectedModel?.defaultParams.height === "number"
          ? String(selectedModel.defaultParams.height)
          : current.height,
      steps:
        typeof selectedModel?.defaultParams.steps === "number"
          ? String(selectedModel.defaultParams.steps)
          : current.steps,
      guidanceScale:
        typeof selectedModel?.defaultParams.guidance_scale === "number"
          ? String(selectedModel.defaultParams.guidance_scale)
          : current.guidanceScale,
      durationSeconds:
        typeof selectedModel?.defaultParams.duration_seconds === "number"
          ? String(selectedModel.defaultParams.duration_seconds)
          : current.durationSeconds,
      cameraMotion:
        typeof selectedModel?.defaultParams.camera_motion === "string"
          ? selectedModel.defaultParams.camera_motion
          : current.cameraMotion,
      visualStyle:
        typeof selectedModel?.defaultParams.visual_style === "string"
          ? selectedModel.defaultParams.visual_style
          : current.visualStyle,
    }));
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    onSubmit?.({
      mediaType,
      modelId: formValues.modelId,
      prompt: formValues.prompt,
      negativePrompt: formValues.negativePrompt,
      width: parseRequiredInteger(formValues.width, defaultValues.width),
      height: parseRequiredInteger(formValues.height, defaultValues.height),
      steps: parseRequiredInteger(formValues.steps, defaultValues.steps),
      guidanceScale: parseRequiredFloat(
        formValues.guidanceScale,
        defaultValues.guidanceScale,
      ),
      loraPath: formValues.loraPath.trim(),
      loraScale: parseRequiredFloat(formValues.loraScale, defaultValues.loraScale),
      seed: parseOptionalInteger(formValues.seed),
      durationSeconds: parseRequiredInteger(
        formValues.durationSeconds,
        defaultValues.durationSeconds,
      ),
      bpm: parseRequiredInteger(formValues.bpm, defaultValues.bpm),
      mood: formValues.mood,
      cameraMotion: formValues.cameraMotion,
      visualStyle: formValues.visualStyle,
    });
  };

  const renderPromptSection = () => (
    <section className="form-section">
      <div className="form-section__header">
        <div>
          <p className="eyebrow">Prompt</p>
          <h3>
            {mediaType === "image"
              ? "Fix the subject before tuning the render"
              : mediaType === "audio"
                ? "Fix the mood before tuning the loop"
                : "Fix the shot intent before tuning motion"}
          </h3>
        </div>
        <span className="form-section__mode">
          {controlMode === "quick" ? "quick pass" : "detail pass"}
        </span>
      </div>

      <label className="field-group field-group--full">
        <span>Prompt</span>
        <textarea
          name="prompt"
          rows={controlMode === "quick" ? 4 : 5}
          placeholder={
            mediaType === "image"
              ? "Describe the image you want to generate"
              : mediaType === "audio"
                ? "Describe the music you want to generate"
                : "Describe the motion board or storyboard you want to generate"
          }
          value={formValues.prompt}
          onChange={(event) => setFieldValue("prompt", event.target.value)}
          disabled={disabled}
        />
      </label>

      {(controlMode === "advanced" || mediaType === "video") &&
      (mediaType === "image" || mediaType === "video") ? (
        <label className="field-group field-group--full">
          <span>Negative Prompt</span>
          <textarea
            name="negativePrompt"
            rows={4}
            placeholder="Describe what to avoid"
            value={formValues.negativePrompt}
            onChange={(event) => setFieldValue("negativePrompt", event.target.value)}
            disabled={disabled}
          />
        </label>
      ) : null}
    </section>
  );

  const renderQuickControls = () => {
    if (mediaType === "image") {
      return (
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <p className="eyebrow">Quick Controls</p>
              <h3>Choose format and style with minimal decisions</h3>
            </div>
          </div>

          <div className="field-grid field-grid--balanced">
            <label className="field-group">
              <span>Format</span>
              <select
                name="imageFormat"
                value={imageFormatValue}
                onChange={(event) => {
                  const nextPreset = imageFormatPresets.find(
                    (preset) => preset.value === event.target.value,
                  );
                  if (!nextPreset) {
                    return;
                  }
                  setFieldValue("width", String(nextPreset.width));
                  setFieldValue("height", String(nextPreset.height));
                }}
                disabled={disabled}
              >
                {imageFormatPresets.map((preset) => (
                  <option key={preset.value} value={preset.value}>
                    {preset.label}
                  </option>
                ))}
                <option value="custom">Custom</option>
              </select>
            </label>

            {loraOptions.length > 0 ? (
              <label className="field-group">
                <span>Style Preset</span>
                <select
                  name="loraPreset"
                  value={
                    loraOptions.some((option) => option.path === formValues.loraPath)
                      ? formValues.loraPath
                      : ""
                  }
                  onChange={(event) => setFieldValue("loraPath", event.target.value)}
                  disabled={disabled}
                >
                  <option value="">Base model only</option>
                  {loraOptions.map((option) => (
                    <option key={option.id} value={option.path}>
                      {option.displayName}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
          </div>
        </section>
      );
    }

    if (mediaType === "audio") {
      return (
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <p className="eyebrow">Quick Controls</p>
              <h3>Set duration, bpm, and mood for immediate playback</h3>
            </div>
          </div>

          <div className="field-grid field-grid--controls">
            <label className="field-group">
              <span>Duration (sec)</span>
              <input
                type="number"
                inputMode="numeric"
                min="2"
                step="1"
                name="durationSeconds"
                value={formValues.durationSeconds}
                onChange={(event) => setFieldValue("durationSeconds", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>BPM</span>
              <input
                type="number"
                inputMode="numeric"
                min="60"
                max="180"
                step="1"
                name="bpm"
                value={formValues.bpm}
                onChange={(event) => setFieldValue("bpm", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Mood</span>
              <select
                name="mood"
                value={formValues.mood}
                onChange={(event) => setFieldValue("mood", event.target.value)}
                disabled={disabled}
              >
                <option value="dreamy">Dreamy</option>
                <option value="bright">Bright</option>
                <option value="dark">Dark</option>
                <option value="energetic">Energetic</option>
                <option value="gentle">Gentle</option>
              </select>
            </label>
          </div>
        </section>
      );
    }

    return (
      <section className="form-section">
        <div className="form-section__header">
          <div>
            <p className="eyebrow">Quick Controls</p>
            <h3>Set reel length, camera motion, and board style first</h3>
          </div>
        </div>

        <div className="field-grid field-grid--controls">
          <label className="field-group">
            <span>Duration (sec)</span>
            <input
              type="number"
              inputMode="numeric"
              min="2"
              step="1"
              name="durationSeconds"
              value={formValues.durationSeconds}
              onChange={(event) => setFieldValue("durationSeconds", event.target.value)}
              disabled={disabled}
            />
          </label>
          <label className="field-group">
            <span>Camera Motion</span>
            <select
              name="cameraMotion"
              value={formValues.cameraMotion}
              onChange={(event) => setFieldValue("cameraMotion", event.target.value)}
              disabled={disabled}
            >
              <option value="push-in">Push In</option>
              <option value="orbit">Orbit</option>
              <option value="tilt-up">Tilt Up</option>
              <option value="lateral">Lateral</option>
            </select>
          </label>
          <label className="field-group">
            <span>Board Style</span>
            <select
              name="visualStyle"
              value={formValues.visualStyle}
              onChange={(event) => setFieldValue("visualStyle", event.target.value)}
              disabled={disabled}
            >
              <option value="storyboard">Storyboard</option>
              <option value="editorial-board">Editorial Board</option>
              <option value="animatic">Animatic</option>
              <option value="blocking-pass">Blocking Pass</option>
            </select>
          </label>
        </div>
      </section>
    );
  };

  const renderAdvancedControls = () => {
    if (mediaType === "image") {
      return (
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <p className="eyebrow">Advanced Controls</p>
              <h3>Fine tune render size, diffusion, seed, and LoRA intensity</h3>
            </div>
          </div>

          <div className="field-grid field-grid--controls">
            <label className="field-group">
              <span>Width</span>
              <input
                type="number"
                inputMode="numeric"
                min="64"
                step="64"
                name="width"
                value={formValues.width}
                onChange={(event) => setFieldValue("width", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Height</span>
              <input
                type="number"
                inputMode="numeric"
                min="64"
                step="64"
                name="height"
                value={formValues.height}
                onChange={(event) => setFieldValue("height", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Steps</span>
              <input
                type="number"
                inputMode="numeric"
                min="1"
                step="1"
                name="steps"
                value={formValues.steps}
                onChange={(event) => setFieldValue("steps", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Guidance Scale</span>
              <input
                type="number"
                inputMode="decimal"
                min="1"
                step="0.1"
                name="guidanceScale"
                value={formValues.guidanceScale}
                onChange={(event) => setFieldValue("guidanceScale", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Seed</span>
              <input
                type="number"
                inputMode="numeric"
                step="1"
                name="seed"
                placeholder="Random"
                value={formValues.seed}
                onChange={(event) => setFieldValue("seed", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>LoRA Scale</span>
              <input
                type="number"
                inputMode="decimal"
                min="0"
                step="0.05"
                name="loraScale"
                value={formValues.loraScale}
                onChange={(event) => setFieldValue("loraScale", event.target.value)}
                disabled={disabled}
              />
            </label>
          </div>

          <label className="field-group field-group--full">
            <span>LoRA Path</span>
            <input
              type="text"
              name="loraPath"
              placeholder="./models/loras/mai.safetensors"
              value={formValues.loraPath}
              onChange={(event) => setFieldValue("loraPath", event.target.value)}
              disabled={disabled}
            />
          </label>
        </section>
      );
    }

    if (mediaType === "audio") {
      return (
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <p className="eyebrow">Advanced Controls</p>
              <h3>Expose guidance and seed only when you need repeatability</h3>
            </div>
          </div>

          <div className="field-grid field-grid--balanced">
            <label className="field-group">
              <span>Guidance Scale</span>
              <input
                type="number"
                inputMode="decimal"
                min="1"
                step="0.1"
                name="guidanceScale"
                value={formValues.guidanceScale}
                onChange={(event) => setFieldValue("guidanceScale", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Seed</span>
              <input
                type="number"
                inputMode="numeric"
                step="1"
                name="seed"
                placeholder="Random"
                value={formValues.seed}
                onChange={(event) => setFieldValue("seed", event.target.value)}
                disabled={disabled}
              />
            </label>
          </div>
        </section>
      );
    }

    return (
      <section className="form-section">
        <div className="form-section__header">
          <div>
            <p className="eyebrow">Advanced Controls</p>
            <h3>Use custom frame size, negative prompt, and seed for precise reels</h3>
          </div>
        </div>

        <div className="field-grid field-grid--controls">
          <label className="field-group">
            <span>Width</span>
            <input
              type="number"
              inputMode="numeric"
              min="256"
              step="64"
              name="width"
              value={formValues.width}
              onChange={(event) => setFieldValue("width", event.target.value)}
              disabled={disabled}
            />
          </label>
          <label className="field-group">
            <span>Height</span>
            <input
              type="number"
              inputMode="numeric"
              min="256"
              step="64"
              name="height"
              value={formValues.height}
              onChange={(event) => setFieldValue("height", event.target.value)}
              disabled={disabled}
            />
          </label>
          <label className="field-group">
            <span>Seed</span>
            <input
              type="number"
              inputMode="numeric"
              step="1"
              name="seed"
              placeholder="Random"
              value={formValues.seed}
              onChange={(event) => setFieldValue("seed", event.target.value)}
              disabled={disabled}
            />
          </label>
        </div>
      </section>
    );
  };

  return (
    <form id={formId} className="prompt-form" onSubmit={handleSubmit}>
      <div className="form-toolbar">
        <div>
          <p className="eyebrow">
            {mediaType === "image"
              ? "Image Blueprint"
              : mediaType === "audio"
                ? "Music Blueprint"
                : "Video Blueprint"}
          </p>
          <p className="toolbar-copy">
            {mediaType === "image"
              ? "短い特徴語で主題を固定し、checkpoint と LoRA でテイストを寄せます。"
              : mediaType === "audio"
                ? "雰囲気、テンポ、長さを先に決めて、すぐに再生できるローカルループを作ります。"
                : "プロンプトを storyboard reel に変換し、構図とカメラ感だけを素早く確認します。"}
          </p>
        </div>

        <div className="toolbar-actions">
          <div className="preset-row" role="group" aria-label="Prompt presets">
            {mediaType === "image"
              ? imagePresets.map((preset) => (
                  <button
                    key={preset.name}
                    type="button"
                    className="secondary-button"
                    onClick={() => applyImagePreset(preset)}
                    disabled={disabled}
                  >
                    {preset.name}
                  </button>
                ))
              : mediaType === "audio"
                ? audioPresets.map((preset) => (
                    <button
                      key={preset.name}
                      type="button"
                      className="secondary-button"
                      onClick={() => applyAudioPreset(preset)}
                      disabled={disabled}
                    >
                      {preset.name}
                    </button>
                  ))
                : videoPresets.map((preset) => (
                    <button
                      key={preset.name}
                      type="button"
                      className="secondary-button"
                      onClick={() => applyVideoPreset(preset)}
                      disabled={disabled}
                    >
                      {preset.name}
                    </button>
                  ))}
          </div>

          <div className="mode-toggle" role="tablist" aria-label="Control density">
            <button
              type="button"
              role="tab"
              aria-selected={controlMode === "quick"}
              className={`mode-toggle__button ${
                controlMode === "quick" ? "is-active" : ""
              }`}
              onClick={() => setControlMode("quick")}
              disabled={disabled}
            >
              Quick
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={controlMode === "advanced"}
              className={`mode-toggle__button ${
                controlMode === "advanced" ? "is-active" : ""
              }`}
              onClick={() => setControlMode("advanced")}
              disabled={disabled}
            >
              Advanced
            </button>
          </div>
        </div>
      </div>

      {statusMessage ? <p className="form-status">{statusMessage}</p> : null}

      <div className="form-grid">
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <p className="eyebrow">Source Setup</p>
              <h3>
                {mediaType === "image"
                  ? "Pick the model and style source"
                  : mediaType === "audio"
                    ? "Pick the model and playback base"
                    : "Pick the video runtime and storyboard base"}
              </h3>
            </div>
            {modelOptions.length > 0 ? (
              <div className="form-inline-stats" aria-label="Model availability summary">
                <span className="form-inline-stats__item">
                  Installed {availableModelCount}/{modelOptions.length}
                </span>
                {unavailableModelCount > 0 ? (
                  <span className="form-inline-stats__item">
                    Missing {unavailableModelCount}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>

          <div className="field-grid field-grid--balanced">
            {modelOptions.length > 0 ? (
              <label className="field-group field-group--full">
                <span>Model</span>
                <select
                  name="modelId"
                  value={formValues.modelId}
                  onChange={(event) => handleModelChange(event.target.value)}
                  disabled={disabled}
                >
                  {modelOptions.map((option) => (
                    <option
                      key={option.id}
                      value={option.id}
                      disabled={!option.isAvailable}
                    >
                      {option.displayName}
                      {option.isAvailable ? "" : " (not installed)"}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}

            {controlMode === "advanced" && mediaType === "image" && loraOptions.length > 0 ? (
              <label className="field-group field-group--full">
                <span>LoRA Catalog</span>
                <select
                  name="loraPreset"
                  value={
                    loraOptions.some((option) => option.path === formValues.loraPath)
                      ? formValues.loraPath
                      : ""
                  }
                  onChange={(event) => setFieldValue("loraPath", event.target.value)}
                  disabled={disabled}
                >
                  <option value="">None</option>
                  {loraOptions.map((option) => (
                    <option key={option.id} value={option.path}>
                      {option.displayName} ({option.relativePath})
                    </option>
                  ))}
                </select>
                <small className="field-help">
                  ローカル配置済み LoRA を選択できます。必要なら下で手入力も可能です。
                </small>
              </label>
            ) : null}
          </div>

          {missingModelGuides.length > 0 ? (
            <div className="download-guide-list" aria-label="Model download guides">
              {missingModelGuides.map((item) => (
                <div key={item.modelId} className="download-guide">
                  <div>
                    <strong>{item.displayName}</strong>
                    <p>{item.guide.note}</p>
                  </div>
                  <a href={item.guide.url} target="_blank" rel="noreferrer">
                    Download
                  </a>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        {renderPromptSection()}
        {controlMode === "quick" ? renderQuickControls() : renderAdvancedControls()}
      </div>

      <div className="form-actions form-actions--footer">
        <button type="submit" disabled={disabled || !canSubmit}>
          {submitLabel}
        </button>
      </div>
    </form>
  );
}
