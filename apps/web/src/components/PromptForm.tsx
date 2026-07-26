import { useEffect, useState, type FormEvent } from "react";
import {
  audioGenreOptions,
  audioMoodOptions,
  audioPresets,
  audioStructureOptions,
  defaultPromptFormValues,
  imageFormatPresets,
  imagePresets,
  videoPresets,
} from "./promptFormConfig";
import type {
  AudioPreset,
  ControlMode,
  ImagePreset,
  MediaType,
  ModelInstallGuide,
  ModelOption,
  PromptFormProps,
  PromptFormState,
  PromptFormSubmitValues,
  VideoPreset,
} from "./promptFormTypes";

export type {
  LoraOption,
  MediaType,
  ModelOption,
  PromptFormProps,
  PromptFormSubmitValues,
} from "./promptFormTypes";

const defaultValues = defaultPromptFormValues;

function hasOption(
  options: ReadonlyArray<{ value: string }>,
  value: string,
): boolean {
  return options.some((option) => option.value === value);
}

function createInitialState(
  initialValues?: Partial<PromptFormSubmitValues>,
): PromptFormState {
  const merged = { ...defaultValues, ...initialValues };

  return {
    modelId: merged.modelId,
    outputFormat: merged.outputFormat,
    prompt: merged.prompt,
    negativePrompt: merged.negativePrompt,
    imageBriefPurpose: merged.imageBriefPurpose,
    imageBriefSubject: merged.imageBriefSubject,
    imageBriefMood: merged.imageBriefMood,
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
    genre: merged.genre,
    instruments: merged.instruments,
    structure: merged.structure,
    temperature: String(merged.temperature),
    topK: String(merged.topK),
    topP: String(merged.topP),
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
    outputFormat: formValues.outputFormat,
    prompt: formValues.prompt,
    negativePrompt: formValues.negativePrompt,
    imageBriefPurpose: formValues.imageBriefPurpose,
    imageBriefSubject: formValues.imageBriefSubject,
    imageBriefMood: formValues.imageBriefMood,
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
    genre: formValues.genre,
    instruments: formValues.instruments,
    structure: formValues.structure,
    temperature: parseRequiredFloat(
      formValues.temperature,
      defaultValues.temperature,
    ),
    topK: parseRequiredInteger(formValues.topK, defaultValues.topK),
    topP: parseRequiredFloat(formValues.topP, defaultValues.topP),
    cameraMotion: formValues.cameraMotion,
    visualStyle: formValues.visualStyle,
  };
}

function buildSimpleImagePrompt(formValues: PromptFormState): string {
  const purpose =
    formValues.imageBriefPurpose === "自由入力" ? "" : formValues.imageBriefPurpose;
  return [formValues.imageBriefSubject.trim(), purpose, formValues.imageBriefMood]
    .filter(Boolean)
    .join(", ");
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

  if (mediaType === "video" && normalizedId.includes("learned-video")) {
    return {
      label: "CogVideoX-2B",
      url: "https://huggingface.co/THUDM/CogVideoX-2b",
      note: "Diffusers形式のweightを `models/video/cogvideox-2b` に配置します。",
    };
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
  const unavailableModels = modelOptions.filter((option) => !option.isAvailable);
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

  useEffect(() => {
    const selectedModel = modelOptions.find((option) => option.id === formValues.modelId);
    if (selectedModel?.isAvailable) {
      return;
    }

    const preferredModel =
      modelOptions.find((option) => option.isAvailable && option.isDefault) ??
      modelOptions.find((option) => option.isAvailable);
    const nextModelId = preferredModel?.id ?? "";
    if (nextModelId === formValues.modelId) {
      return;
    }

    setFormValues((current) => ({
      ...current,
      modelId: nextModelId,
    }));
  }, [formValues.modelId, modelOptions]);

  const setFieldValue = <K extends keyof PromptFormState>(
    field: K,
    value: PromptFormState[K],
  ) => {
    setFormValues((current) => ({
      ...current,
      [field]: value,
    }));
  };

  const applySimpleBrief = () => {
    setFieldValue("prompt", buildSimpleImagePrompt(formValues));
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
      genre: preset.genre,
      instruments: preset.instruments,
      structure: preset.structure,
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
      outputFormat:
        typeof selectedModel?.defaultParams.output_format === "string"
          ? selectedModel.defaultParams.output_format
          : mediaType === "video"
            ? "gif"
            : current.outputFormat,
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
      temperature:
        typeof selectedModel?.defaultParams.temperature === "number"
          ? String(selectedModel.defaultParams.temperature)
          : current.temperature,
      topK:
        typeof selectedModel?.defaultParams.top_k === "number"
          ? String(selectedModel.defaultParams.top_k)
          : current.topK,
      topP:
        typeof selectedModel?.defaultParams.top_p === "number"
          ? String(selectedModel.defaultParams.top_p)
          : current.topP,
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

    const simpleImagePrompt =
      mediaType === "image" && controlMode === "quick"
        ? buildSimpleImagePrompt(formValues)
        : "";

    onSubmit?.({
      mediaType,
      modelId: formValues.modelId,
      outputFormat: formValues.outputFormat,
      prompt: formValues.prompt.trim() || simpleImagePrompt,
      negativePrompt: formValues.negativePrompt,
      imageBriefPurpose: formValues.imageBriefPurpose,
      imageBriefSubject: formValues.imageBriefSubject,
      imageBriefMood: formValues.imageBriefMood,
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
      genre: formValues.genre,
      instruments: formValues.instruments.trim(),
      structure: formValues.structure,
      temperature: parseRequiredFloat(
        formValues.temperature,
        defaultValues.temperature,
      ),
      topK: parseRequiredInteger(formValues.topK, defaultValues.topK),
      topP: parseRequiredFloat(formValues.topP, defaultValues.topP),
      cameraMotion: formValues.cameraMotion,
      visualStyle: formValues.visualStyle,
    });
  };

  const renderPromptSection = () => (
    <section className="form-section">
      <div className="form-section__header">
        <div>
          <h3>Prompt</h3>
          <p>
            {mediaType === "image"
              ? "Fix the subject before tuning the render"
              : mediaType === "audio"
                ? "Fix the mood before tuning the loop"
                : "Fix the shot intent before tuning motion"}
          </p>
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

  const renderSimpleImageBrief = () => {
    if (mediaType !== "image" || controlMode !== "quick") {
      return null;
    }

    return (
      <section className="form-section simple-brief">
        <div className="form-section__header">
          <div>
            <h3>何を作りたいですか？</h3>
            <p>用途、主役、雰囲気を選ぶと、送信する内容を下に組み立てます。</p>
          </div>
        </div>
        <div className="preset-row" role="group" aria-label="制作目的">
          {["SNS投稿", "キャラクター", "商品イメージ", "YouTube サムネイル", "自由入力"].map((purpose) => (
            <button
              key={purpose}
              type="button"
              className={`secondary-button ${formValues.imageBriefPurpose === purpose ? "is-selected" : ""}`}
              onClick={() => setFieldValue("imageBriefPurpose", purpose)}
              aria-pressed={formValues.imageBriefPurpose === purpose}
              disabled={disabled}
            >
              {purpose}
            </button>
          ))}
        </div>
        <div className="field-grid field-grid--balanced">
          <label className="field-group">
            <span>1. 主役・内容</span>
            <input
              type="text"
              value={formValues.imageBriefSubject}
              onChange={(event) => setFieldValue("imageBriefSubject", event.target.value)}
              placeholder="例：夕暮れのカフェで本を読む人物"
              disabled={disabled}
            />
          </label>
          <label className="field-group">
            <span>2. 雰囲気</span>
            <select
              value={formValues.imageBriefMood}
              onChange={(event) => setFieldValue("imageBriefMood", event.target.value)}
              disabled={disabled}
            >
              <option value="やわらかい光">やわらかい光</option>
              <option value="明るくクリーン">明るくクリーン</option>
              <option value="映画のような光">映画のような光</option>
              <option value="落ち着いたトーン">落ち着いたトーン</option>
            </select>
          </label>
        </div>
        <label className="field-group field-group--full">
          <span>避けたい要素</span>
          <input
            type="text"
            value={formValues.negativePrompt}
            onChange={(event) => setFieldValue("negativePrompt", event.target.value)}
            placeholder="例：文字、ぼやけ、余分な人物"
            disabled={disabled}
          />
        </label>
        <div className="simple-brief__summary">
          <div>
            <span>作られる内容の要約</span>
            <strong>{[
              formValues.imageBriefSubject || "主役を入力",
              formValues.imageBriefPurpose,
              formValues.imageBriefMood,
            ].join("、")}</strong>
          </div>
          <button
            type="button"
            className="secondary-button"
            onClick={applySimpleBrief}
            disabled={disabled || !formValues.imageBriefSubject.trim()}
          >
            プロンプトに反映
          </button>
        </div>
      </section>
    );
  };

  const renderQuickControls = () => {
    if (mediaType === "image") {
      return (
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <h3>Quick Controls</h3>
              <p>Choose format and style with minimal decisions</p>
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
              <h3>Quick Controls</h3>
              <p>Shape the tempo, palette, and arrangement before generation</p>
            </div>
          </div>

          <div className="field-grid field-grid--controls">
            <label className="field-group">
              <span>Duration (sec)</span>
              <input
                type="number"
                inputMode="numeric"
                min="2"
                max="30"
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
                min="40"
                max="240"
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
                {formValues.mood && !hasOption(audioMoodOptions, formValues.mood) ? (
                  <option value={formValues.mood}>{formValues.mood} (restored)</option>
                ) : null}
                {audioMoodOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field-group">
              <span>Genre</span>
              <select
                name="genre"
                value={formValues.genre}
                onChange={(event) => setFieldValue("genre", event.target.value)}
                disabled={disabled}
              >
                {formValues.genre && !hasOption(audioGenreOptions, formValues.genre) ? (
                  <option value={formValues.genre}>{formValues.genre} (restored)</option>
                ) : null}
                {audioGenreOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field-group">
              <span>Structure</span>
              <select
                name="structure"
                value={formValues.structure}
                onChange={(event) => setFieldValue("structure", event.target.value)}
                disabled={disabled}
              >
                {formValues.structure &&
                !hasOption(audioStructureOptions, formValues.structure) ? (
                  <option value={formValues.structure}>
                    {formValues.structure} (restored)
                  </option>
                ) : null}
                {audioStructureOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <label className="field-group field-group--full">
            <span>Instruments</span>
            <input
              type="text"
              name="instruments"
              aria-label="Instruments"
              aria-describedby="audio-instruments-help"
              placeholder="warm synth, soft percussion, electric bass"
              value={formValues.instruments}
              onChange={(event) => setFieldValue("instruments", event.target.value)}
              disabled={disabled}
            />
            <small id="audio-instruments-help" className="field-help">
              カンマ区切りの楽器指定を、ジャンル・構成・BPMと一緒にMusicGenへ渡します。
            </small>
          </label>
        </section>
      );
    }

    return (
      <section className="form-section">
        <div className="form-section__header">
          <div>
            <h3>Quick Controls</h3>
            <p>Set reel length, camera motion, and board style first</p>
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
              <h3>Advanced Controls</h3>
              <p>Fine tune render size, diffusion, seed, and LoRA intensity</p>
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
              <h3>Advanced Controls</h3>
              <p>Tune prompt adherence, variation, and sampling diversity</p>
            </div>
          </div>

          <div className="field-grid field-grid--balanced">
            <label className="field-group">
              <span>Guidance Scale</span>
              <input
                type="number"
                inputMode="decimal"
                min="1"
                max="10"
                step="0.1"
                name="guidanceScale"
                value={formValues.guidanceScale}
                onChange={(event) => setFieldValue("guidanceScale", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Temperature</span>
              <input
                type="number"
                inputMode="decimal"
                min="0.1"
                max="2"
                step="0.1"
                name="temperature"
                value={formValues.temperature}
                onChange={(event) => setFieldValue("temperature", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Top K</span>
              <input
                type="number"
                inputMode="numeric"
                min="0"
                max="1000"
                step="1"
                name="topK"
                value={formValues.topK}
                onChange={(event) => setFieldValue("topK", event.target.value)}
                disabled={disabled}
              />
            </label>
            <label className="field-group">
              <span>Top P</span>
              <input
                type="number"
                inputMode="decimal"
                min="0"
                max="1"
                step="0.05"
                name="topP"
                value={formValues.topP}
                onChange={(event) => setFieldValue("topP", event.target.value)}
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
            <h3>Advanced Controls</h3>
            <p>Use custom frame size, negative prompt, and seed for precise reels</p>
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

      {statusMessage ? (
        <p
          className="form-status"
          role="status"
          aria-live="polite"
        >
          {statusMessage}
        </p>
      ) : null}

      <div className="form-grid">
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <h3>
                {mediaType === "image"
                  ? "Pick the model and style source"
                  : mediaType === "audio"
                    ? "Pick the model and playback base"
                    : "Pick the video runtime and storyboard base"}
              </h3>
              <p>
                {mediaType === "image"
                  ? "Select a model to generate your content"
                  : mediaType === "audio"
                    ? "Choose a model for audio generation"
                    : "Select a video model and storyboard base"}
              </p>
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
                  disabled={disabled || availableModelCount === 0}
                >
                  {availableModelCount === 0 ? (
                    <option value="">No available local models</option>
                  ) : null}
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

          {unavailableModels.length > 0 ? (
            <div className="model-availability-list" role="status" aria-live="polite">
              {unavailableModels.map((option) => (
                <p key={option.id}>
                  <strong>{option.displayName}:</strong>{" "}
                  {option.availabilityMessage || "Required local model files are missing."}
                </p>
              ))}
            </div>
          ) : null}

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

        {renderSimpleImageBrief()}
        {renderPromptSection()}
        {controlMode === "quick" ? renderQuickControls() : renderAdvancedControls()}
      </div>

      <div className="form-actions form-actions--footer">
        <button 
          type="submit" 
          disabled={disabled || !canSubmit}
        >
          {submitLabel}
        </button>
      </div>
    </form>
  );
}
