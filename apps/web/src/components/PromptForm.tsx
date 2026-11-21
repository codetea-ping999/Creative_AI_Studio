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
  PromptFormProps,
  PromptFormState,
  PromptFormSubmitValues,
  VideoPreset,
} from "./promptFormTypes";
import { useT } from "../i18n";

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
    variationCount: String(merged.variationCount),
    durationSeconds: String(merged.durationSeconds),
    extendStrideSeconds:
      merged.extendStrideSeconds === null ? "" : String(merged.extendStrideSeconds),
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
    variationCount: parseRequiredInteger(
      formValues.variationCount,
      defaultValues.variationCount,
    ),
    durationSeconds: parseRequiredInteger(
      formValues.durationSeconds,
      defaultValues.durationSeconds,
    ),
    extendStrideSeconds: parseOptionalInteger(formValues.extendStrideSeconds),
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

export function PromptForm({
  formId,
  mediaType = "image",
  modelOptions = [],
  loraOptions = [],
  initialValues,
  submitLabel,
  disabled = false,
  canSubmit = true,
  statusMessage = null,
  onSubmit,
  onDraftChange,
}: PromptFormProps) {
  const t = useT();
  const [formValues, setFormValues] = useState<PromptFormState>(() =>
    createInitialState(initialValues),
  );
  const [controlMode, setControlMode] = useState<ControlMode>("quick");
  const negativePromptUnsupported =
    mediaType === "image" &&
    modelOptions.find((option) => option.id === formValues.modelId)?.supportsNegativePrompt ===
      false;
  const renderNegativePromptNote = () =>
    negativePromptUnsupported ? (
      <small
        id="negative-prompt-unsupported"
        className="field-help field-group--full"
        role="note"
      >
        {t(
          "このモデル（{model}）は避けたい要素に対応していないため、入力しても生成には使われません。",
          { model: formValues.modelId },
        )}
      </small>
    ) : null;
  const availableModelCount = modelOptions.filter((option) => option.isAvailable).length;
  const unavailableModelCount = modelOptions.length - availableModelCount;
  const unavailableModels = modelOptions.filter((option) => !option.isAvailable);
  const selectedModel = modelOptions.find(
    (option) => option.id === formValues.modelId,
  );
  const isLongFormAudio =
    mediaType === "audio" && Boolean(selectedModel?.tags.includes("long-form"));
  const imageFormatValue = resolveImageFormatPreset(
    parseRequiredInteger(formValues.width, defaultValues.width),
    parseRequiredInteger(formValues.height, defaultValues.height),
  );

  useEffect(() => {
    onDraftChange?.(serializeDraft(mediaType, formValues));
  }, [formValues, mediaType, onDraftChange]);

  useEffect(() => {
    const selectedModel = modelOptions.find((option) => option.id === formValues.modelId);
    if (selectedModel?.isAvailable) {
      if (mediaType === "audio") {
        const longForm = selectedModel.tags.includes("long-form");
        const duration = Number.parseInt(formValues.durationSeconds, 10);
        const minimum = longForm ? 31 : 2;
        const maximum = longForm ? 120 : 30;
        const stride = Number.parseInt(formValues.extendStrideSeconds, 10);
        const invalidDuration =
          !Number.isFinite(duration) || duration < minimum || duration > maximum;
        const invalidStride =
          longForm && (!Number.isFinite(stride) || stride < 5 || stride > 29);
        if (invalidDuration || invalidStride || (!longForm && formValues.extendStrideSeconds)) {
          setFormValues((current) => ({
            ...current,
            durationSeconds: invalidDuration
              ? String(selectedModel.defaultParams.duration_seconds ?? minimum)
              : current.durationSeconds,
            extendStrideSeconds: longForm
              ? invalidStride
                ? String(selectedModel.defaultParams.extend_stride_seconds ?? 18)
                : current.extendStrideSeconds
              : "",
          }));
        }
      }
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
      durationSeconds:
        typeof preferredModel?.defaultParams.duration_seconds === "number"
          ? String(preferredModel.defaultParams.duration_seconds)
          : current.durationSeconds,
      extendStrideSeconds:
        preferredModel?.tags.includes("long-form") &&
        typeof preferredModel.defaultParams.extend_stride_seconds === "number"
          ? String(preferredModel.defaultParams.extend_stride_seconds)
          : "",
    }));
  }, [formValues.modelId, mediaType, modelOptions]);

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
      durationSeconds: isLongFormAudio
        ? current.durationSeconds
        : String(preset.durationSeconds),
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
      extendStrideSeconds:
        selectedModel?.tags.includes("long-form") &&
        typeof selectedModel.defaultParams.extend_stride_seconds === "number"
          ? String(selectedModel.defaultParams.extend_stride_seconds)
          : "",
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
      variationCount: parseRequiredInteger(
        formValues.variationCount,
        defaultValues.variationCount,
      ),
      durationSeconds: parseRequiredInteger(
        formValues.durationSeconds,
        defaultValues.durationSeconds,
      ),
      extendStrideSeconds: isLongFormAudio
        ? parseRequiredInteger(formValues.extendStrideSeconds, 18)
        : null,
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
          <h3>{t("プロンプト")}</h3>
          <p>
            {mediaType === "image"
              ? t("細かな設定の前に、まず主役を言葉で決めます")
              : mediaType === "audio"
                ? t("細かな設定の前に、まず雰囲気を言葉で決めます")
                : t("細かな設定の前に、まず見せたい場面を言葉で決めます")}
          </p>
        </div>
      </div>

      <label className="field-group field-group--full">
        <span>{t("プロンプト")}</span>
        <textarea
          name="prompt"
          rows={controlMode === "quick" ? 4 : 5}
          placeholder={
            mediaType === "image"
              ? t("作りたい画像を書いてください")
              : mediaType === "audio"
                ? t("作りたい音楽を書いてください")
                : t("作りたい動画（絵コンテ）の場面を書いてください")
          }
          value={formValues.prompt}
          onChange={(event) => setFieldValue("prompt", event.target.value)}
          disabled={disabled}
        />
      </label>

      {(controlMode === "advanced" || mediaType === "video") &&
      (mediaType === "image" || mediaType === "video") ? (
        <>
          <label className="field-group field-group--full">
            <span>{t("避けたい要素")}</span>
            <textarea
              name="negativePrompt"
              rows={4}
              placeholder={t("例：ぼやけ、余分な人物、文字")}
              value={formValues.negativePrompt}
              onChange={(event) => setFieldValue("negativePrompt", event.target.value)}
              disabled={disabled}
              aria-describedby={negativePromptUnsupported ? "negative-prompt-unsupported" : undefined}
            />
          </label>
          {renderNegativePromptNote()}
        </>
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
            <h3>{t("何を作りたいですか？")}</h3>
            <p>{t("用途、主役、雰囲気を選ぶと、送信する内容を下に組み立てます。")}</p>
          </div>
        </div>
        <div className="preset-row" role="group" aria-label={t("制作目的")}>
          {["SNS投稿", "キャラクター", "商品イメージ", "YouTube サムネイル", "自由入力"].map((purpose) => (
            <button
              key={purpose}
              type="button"
              className={`secondary-button ${formValues.imageBriefPurpose === purpose ? "is-selected" : ""}`}
              onClick={() => setFieldValue("imageBriefPurpose", purpose)}
              aria-pressed={formValues.imageBriefPurpose === purpose}
              disabled={disabled}
            >
              {t(purpose)}
            </button>
          ))}
        </div>
        <div className="field-grid field-grid--balanced">
          <label className="field-group">
            <span>{t("1. 主役・内容")}</span>
            <input
              type="text"
              value={formValues.imageBriefSubject}
              onChange={(event) => setFieldValue("imageBriefSubject", event.target.value)}
              placeholder={t("例：夕暮れのカフェで本を読む人物")}
              disabled={disabled}
            />
          </label>
          <label className="field-group">
            <span>{t("2. 雰囲気")}</span>
            <select
              value={formValues.imageBriefMood}
              onChange={(event) => setFieldValue("imageBriefMood", event.target.value)}
              disabled={disabled}
            >
              <option value="やわらかい光">{t("やわらかい光")}</option>
              <option value="明るくクリーン">{t("明るくクリーン")}</option>
              <option value="映画のような光">{t("映画のような光")}</option>
              <option value="落ち着いたトーン">{t("落ち着いたトーン")}</option>
            </select>
          </label>
        </div>
        <label className="field-group field-group--full">
          <span>{t("避けたい要素")}</span>
          <input
            type="text"
            value={formValues.negativePrompt}
            onChange={(event) => setFieldValue("negativePrompt", event.target.value)}
            placeholder={t("例：文字、ぼやけ、余分な人物")}
            disabled={disabled}
            aria-describedby={negativePromptUnsupported ? "negative-prompt-unsupported" : undefined}
          />
        </label>
        {renderNegativePromptNote()}
        <div className="simple-brief__summary">
          <div>
            <span>{t("作られる内容の要約")}</span>
            <strong>{[
              formValues.imageBriefSubject || t("主役を入力"),
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
            {t("プロンプトに反映")}
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
              <h3>{t("基本の設定")}</h3>
              <p>{t("縦横比と画風だけ決めれば作れます")}</p>
            </div>
          </div>

          <div className="field-grid field-grid--balanced">
            <label className="field-group">
              <span>{t("縦横比")}</span>
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
                    {t(preset.label)}
                  </option>
                ))}
                <option value="custom">{t("カスタム")}</option>
              </select>
            </label>

            {loraOptions.length > 0 ? (
              <label className="field-group">
                <span>{t("画風（LoRA）")}</span>
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
                  <option value="">{t("ベースモデルのみ")}</option>
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
              <h3>{t("基本の設定")}</h3>
              <p>{t("長さ、テンポ、雰囲気、構成を決めます")}</p>
            </div>
          </div>

          <div className="field-grid field-grid--controls">
            <label className="field-group">
              <span>{t("長さ（秒）")}</span>
              <input
                type="number"
                inputMode="numeric"
                min={isLongFormAudio ? "31" : "2"}
                max={isLongFormAudio ? "120" : "30"}
                step="1"
                name="durationSeconds"
                value={formValues.durationSeconds}
                onChange={(event) => setFieldValue("durationSeconds", event.target.value)}
                disabled={disabled}
              />
            </label>
            {isLongFormAudio ? (
              <label className="field-group">
                <span>{t("延長の間隔（秒）")}</span>
                <input
                  type="number"
                  inputMode="numeric"
                  min="5"
                  max="29"
                  step="1"
                  name="extendStrideSeconds"
                  aria-label={t("延長の間隔（秒）")}
                  aria-describedby="audio-extend-stride-help"
                  value={formValues.extendStrideSeconds}
                  onChange={(event) =>
                    setFieldValue("extendStrideSeconds", event.target.value)
                  }
                  disabled={disabled}
                />
                <small id="audio-extend-stride-help" className="field-help">
                  {t("生成区間を重ねる間隔です。小さいほど文脈を保ちますが処理時間が増えます。")}
                </small>
              </label>
            ) : null}
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
              <span>{t("雰囲気")}</span>
              <select
                name="mood"
                value={formValues.mood}
                onChange={(event) => setFieldValue("mood", event.target.value)}
                disabled={disabled}
              >
                {formValues.mood && !hasOption(audioMoodOptions, formValues.mood) ? (
                  <option value={formValues.mood}>{t("{value}（復元）", { value: formValues.mood })}</option>
                ) : null}
                {audioMoodOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {t(option.label)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field-group">
              <span>{t("ジャンル")}</span>
              <select
                name="genre"
                value={formValues.genre}
                onChange={(event) => setFieldValue("genre", event.target.value)}
                disabled={disabled}
              >
                {formValues.genre && !hasOption(audioGenreOptions, formValues.genre) ? (
                  <option value={formValues.genre}>{t("{value}（復元）", { value: formValues.genre })}</option>
                ) : null}
                {audioGenreOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {t(option.label)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field-group">
              <span>{t("構成")}</span>
              <select
                name="structure"
                value={formValues.structure}
                onChange={(event) => setFieldValue("structure", event.target.value)}
                disabled={disabled}
              >
                {formValues.structure &&
                !hasOption(audioStructureOptions, formValues.structure) ? (
                  <option value={formValues.structure}>
                    {t("{value}（復元）", { value: formValues.structure })}
                  </option>
                ) : null}
                {audioStructureOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {t(option.label)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <label className="field-group field-group--full">
            <span>{t("楽器")}</span>
            <input
              type="text"
              name="instruments"
              aria-label={t("楽器")}
              aria-describedby="audio-instruments-help"
              placeholder={t("例：warm synth, soft percussion, electric bass")}
              value={formValues.instruments}
              onChange={(event) => setFieldValue("instruments", event.target.value)}
              disabled={disabled}
            />
            <small id="audio-instruments-help" className="field-help">
              {t("カンマ区切りの楽器指定を、ジャンル・構成・BPMと一緒にMusicGenへ渡します。")}
            </small>
          </label>
        </section>
      );
    }

    return (
      <section className="form-section">
        <div className="form-section__header">
          <div>
            <h3>{t("基本の設定")}</h3>
            <p>{t("長さ、カメラの動き、絵コンテの見た目を決めます")}</p>
          </div>
        </div>

        <div className="field-grid field-grid--controls">
          <label className="field-group">
            <span>{t("長さ（秒）")}</span>
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
            <span>{t("カメラの動き")}</span>
            <select
              name="cameraMotion"
              value={formValues.cameraMotion}
              onChange={(event) => setFieldValue("cameraMotion", event.target.value)}
              disabled={disabled}
            >
              <option value="push-in">{t("寄る")}</option>
              <option value="orbit">{t("回り込む")}</option>
              <option value="tilt-up">{t("見上げる")}</option>
              <option value="lateral">{t("横に流す")}</option>
            </select>
          </label>
          <label className="field-group">
            <span>{t("絵コンテの見た目")}</span>
            <select
              name="visualStyle"
              value={formValues.visualStyle}
              onChange={(event) => setFieldValue("visualStyle", event.target.value)}
              disabled={disabled}
            >
              <option value="storyboard">{t("絵コンテ")}</option>
              <option value="editorial-board">{t("エディトリアル")}</option>
              <option value="animatic">{t("アニマティック")}</option>
              <option value="blocking-pass">{t("配置確認")}</option>
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
              <h3>{t("詳細設定")}</h3>
              <p>{t("サイズ、ステップ数、シード、LoRA の強さを調整します")}</p>
            </div>
          </div>

          <div className="field-grid field-grid--controls">
            <label className="field-group">
              <span>{t("幅")}</span>
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
              <span>{t("高さ")}</span>
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
              <span>{t("ステップ数")}</span>
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
              <span>{t("プロンプトへの忠実さ")}</span>
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
              <span>{t("シード")}</span>
              <input
                type="number"
                inputMode="numeric"
                step="1"
                name="seed"
                placeholder={t("ランダム")}
                value={formValues.seed}
                onChange={(event) => setFieldValue("seed", event.target.value)}
                disabled={disabled}
              />
            </label>
            <div className="field-group">
              <label htmlFor="variation-count">{t("作る枚数")}</label>
              <input
                id="variation-count"
                type="number"
                inputMode="numeric"
                min="1"
                max="4"
                step="1"
                name="variationCount"
                value={formValues.variationCount}
                onChange={(event) =>
                  setFieldValue("variationCount", event.target.value)
                }
                aria-describedby="variation-count-hint"
                disabled={disabled}
              />
              <small id="variation-count-hint" className="field-hint">
                {t("1〜4 枚。シードを変えて再現できる形で作ります。")}
              </small>
            </div>
            <label className="field-group">
              <span>{t("LoRA の強さ")}</span>
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
            <span>{t("LoRA のパス")}</span>
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
              <h3>{t("詳細設定")}</h3>
              <p>{t("プロンプトへの忠実さと、ばらつきを調整します")}</p>
            </div>
          </div>

          <div className="field-grid field-grid--balanced">
            <label className="field-group">
              <span>{t("プロンプトへの忠実さ")}</span>
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
              <span>{t("ばらつき（Temperature）")}</span>
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
              <span>{t("シード")}</span>
              <input
                type="number"
                inputMode="numeric"
                step="1"
                name="seed"
                placeholder={t("ランダム")}
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
            <h3>{t("詳細設定")}</h3>
            <p>{t("サイズとシードを指定して、同じ結果を作り直せるようにします")}</p>
          </div>
        </div>

        <div className="field-grid field-grid--controls">
          <label className="field-group">
            <span>{t("幅")}</span>
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
            <span>{t("高さ")}</span>
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
            <span>{t("シード")}</span>
            <input
              type="number"
              inputMode="numeric"
              step="1"
              name="seed"
              placeholder={t("ランダム")}
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
              ? t("画像の下書き")
              : mediaType === "audio"
                ? t("音楽の下書き")
                : t("動画の下書き")}
          </p>
          <p className="toolbar-copy">
            {mediaType === "image"
              ? t("短い言葉で主役を決め、モデルと LoRA で画風を寄せます。")
              : mediaType === "audio"
                ? t("雰囲気、テンポ、長さを先に決めて、すぐに再生できるローカルループを作ります。")
                : t("プロンプトを絵コンテ動画にして、構図とカメラの動きをすばやく確かめます。")}
          </p>
        </div>

        <div className="toolbar-actions">
          <div className="preset-row" role="group" aria-label={t("プリセット")}>
            {mediaType === "image"
              ? imagePresets.map((preset) => (
                  <button
                    key={preset.name}
                    type="button"
                    className="secondary-button"
                    onClick={() => applyImagePreset(preset)}
                    disabled={disabled}
                  >
                    {t(preset.name)}
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
                      {t(preset.name)}
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
                      {t(preset.name)}
                    </button>
                  ))}
          </div>

          <div className="mode-toggle" role="tablist" aria-label={t("設定の細かさ")}>
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
              {t("かんたん")}
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
              {t("詳細")}
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
        {renderSimpleImageBrief()}
        {renderPromptSection()}
        {controlMode === "quick" ? renderQuickControls() : renderAdvancedControls()}
        <section className="form-section">
          <div className="form-section__header">
            <div>
              <h3>
                {mediaType === "image"
                  ? t("モデルを選ぶ")
                  : mediaType === "audio"
                    ? t("モデルを選ぶ")
                    : t("モデルを選ぶ")}
              </h3>
              <p>
                {mediaType === "image"
                  ? t("使える画像モデルから選びます")
                  : mediaType === "audio"
                    ? t("使える音声モデルから選びます")
                    : t("使える動画モデルから選びます。重みがなくても絵コンテは作れます")}
              </p>
            </div>
            {modelOptions.length > 0 ? (
              <div className="form-inline-stats" aria-label={t("モデルの配置状況")}>
                <span className="form-inline-stats__item">
                  {t("配置済み {count}/{total}", { count: availableModelCount, total: modelOptions.length })}
                </span>
                {unavailableModelCount > 0 ? (
                  <span className="form-inline-stats__item">
                    {t("未配置 {count}", { count: unavailableModelCount })}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>

          <div className="field-grid field-grid--balanced">
            {modelOptions.length > 0 ? (
              <label className="field-group field-group--full">
                <span>{t("モデル")}</span>
                <select
                  name="modelId"
                  value={formValues.modelId}
                  onChange={(event) => handleModelChange(event.target.value)}
                  disabled={disabled || availableModelCount === 0}
                >
                  {availableModelCount === 0 ? (
                    <option value="">{t("使えるモデルがありません")}</option>
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
                <span>{t("LoRA 一覧")}</span>
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
                  <option value="">{t("使わない")}</option>
                  {loraOptions.map((option) => (
                    <option key={option.id} value={option.path}>
                      {option.displayName} ({option.relativePath})
                    </option>
                  ))}
                </select>
                <small className="field-help">
                  {t("ローカル配置済み LoRA を選択できます。必要なら下で手入力も可能です。")}
                </small>
              </label>
            ) : null}
          </div>

          {unavailableModels.length > 0 ? (
            <p className="model-availability-note" role="status" aria-live="polite">
              {t("未配置: {names}。配置先と必要なファイルは「準備」画面で確認できます。", {
                names: unavailableModels.map((option) => option.displayName).join(t("、")),
              })}
            </p>
          ) : null}
        </section>
      </div>

      <div className="form-actions form-actions--footer">
        <button 
          type="submit" 
          disabled={disabled || !canSubmit}
        >
          {submitLabel ?? t("生成する")}
        </button>
      </div>
    </form>
  );
}
