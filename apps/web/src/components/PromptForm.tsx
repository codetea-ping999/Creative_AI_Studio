import { useEffect, useState, type FormEvent } from "react";
import Alert from "react-bootstrap/Alert";
import Button from "react-bootstrap/Button";
import Form from "react-bootstrap/Form";
import Stack from "react-bootstrap/Stack";
import {
  audioPresets,
  getInstallGuide,
  imagePresets,
  resolveImageFormatPreset,
  videoPresets,
} from "./promptFormConfig";
import {
  PromptFormControlsSection,
  PromptFormModelSection,
  PromptFormPromptSection,
} from "./PromptFormSections";
import type { PromptFormProps } from "./promptFormTypes";
import {
  buildSubmitValues,
  createInitialState,
  parseRequiredInteger,
  serializeDraft,
  type PromptFormState,
} from "./promptFormState";

export type {
  ControlMode,
  LoraOption,
  MediaType,
  ModelOption,
  PromptFormProps,
  PromptFormSubmitValues,
} from "./promptFormTypes";

export function PromptForm({
  formId,
  mediaType = "image",
  controlMode = "quick",
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
    createInitialState(mediaType, initialValues),
  );
  const availableModelCount = modelOptions.filter((option) => option.isAvailable).length;
  const unavailableModelCount = modelOptions.length - availableModelCount;
  const trimmedPrompt = formValues.prompt.trim();
  const isPromptEmpty = trimmedPrompt.length === 0;
  const promptFieldId = formId ? `${formId}-prompt` : undefined;
  const promptHelpId = formId ? `${formId}-prompt-help` : undefined;
  const promptHelpText = isPromptEmpty
    ? "内容を入力してください。空欄では送信できません。"
    : "短くても構いません。主題や雰囲気を一文で入れてから送信してください。";
  const imageFormatValue = resolveImageFormatPreset(
    parseRequiredInteger(formValues.width, 1024),
    parseRequiredInteger(formValues.height, 1024),
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
        guide: NonNullable<ReturnType<typeof getInstallGuide>>;
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

  const applyImagePreset = (preset: (typeof imagePresets)[number]) => {
    setFormValues((current) => ({
      ...current,
      prompt: preset.prompt,
      negativePrompt: preset.negativePrompt,
    }));
  };

  const applyAudioPreset = (preset: (typeof audioPresets)[number]) => {
    setFormValues((current) => ({
      ...current,
      prompt: preset.prompt,
      mood: preset.mood,
      bpm: String(preset.bpm),
      durationSeconds: String(preset.durationSeconds),
    }));
  };

  const applyVideoPreset = (preset: (typeof videoPresets)[number]) => {
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

    if (disabled || !canSubmit || isPromptEmpty) {
      return;
    }

    onSubmit?.(buildSubmitValues(mediaType, formValues));
  };

  return (
    <Form id={formId} className="prompt-form" onSubmit={handleSubmit}>
      <Stack gap={3}>
        <div className="form-toolbar">
          <div>
            <p className="eyebrow">
              {mediaType === "image"
                ? "画像の設計"
                : mediaType === "audio"
                  ? "音声の設計"
                  : "動画の設計"}
            </p>
            <h3 className="form-toolbar__title">
              {mediaType === "image"
                ? "主題とモデルを決めてから生成する"
                : mediaType === "audio"
                  ? "雰囲気を固めてからローカル生成する"
                  : "構図と動きを揃えてからプレビューする"}
            </h3>
            <p className="toolbar-copy">
              {mediaType === "image"
                ? "短い特徴語で主題を固定し、checkpoint と LoRA でテイストを寄せます。"
                : mediaType === "audio"
                  ? "雰囲気、テンポ、長さを先に決めて、すぐに再生できるローカルループを作ります。"
                  : "プロンプトを storyboard reel に変換し、構図とカメラ感だけを素早く確認します。"}
            </p>
          </div>

          <div className="preset-row" role="group" aria-label="Prompt presets">
            {mediaType === "image"
              ? imagePresets.map((preset) => (
                  <Button
                    key={preset.name}
                    type="button"
                    variant="outline-secondary"
                    size="sm"
                    onClick={() => applyImagePreset(preset)}
                    disabled={disabled}
                  >
                    {preset.name}
                  </Button>
                ))
              : mediaType === "audio"
                ? audioPresets.map((preset) => (
                    <Button
                      key={preset.name}
                      type="button"
                      variant="outline-secondary"
                      size="sm"
                      onClick={() => applyAudioPreset(preset)}
                      disabled={disabled}
                    >
                      {preset.name}
                    </Button>
                  ))
                : videoPresets.map((preset) => (
                    <Button
                      key={preset.name}
                      type="button"
                      variant="outline-secondary"
                      size="sm"
                      onClick={() => applyVideoPreset(preset)}
                      disabled={disabled}
                    >
                      {preset.name}
                    </Button>
                  ))}
          </div>
        </div>

        {statusMessage ? (
          <Alert variant="secondary" className="form-status mb-0">
            {statusMessage}
          </Alert>
        ) : null}

        <div className="form-grid">
          <PromptFormModelSection
            mediaType={mediaType}
            controlMode={controlMode}
            modelOptions={modelOptions}
            loraOptions={loraOptions}
            formValues={formValues}
            disabled={disabled}
            availableModelCount={availableModelCount}
            unavailableModelCount={unavailableModelCount}
            missingModelGuides={missingModelGuides}
            onModelChange={handleModelChange}
            onFieldChange={setFieldValue}
          />
          <PromptFormPromptSection
            mediaType={mediaType}
            controlMode={controlMode}
            formValues={formValues}
            disabled={disabled}
            isPromptEmpty={isPromptEmpty}
            promptFieldId={promptFieldId}
            promptHelpId={promptHelpId}
            promptHelpText={promptHelpText}
            onFieldChange={setFieldValue}
          />
          <PromptFormControlsSection
            mediaType={mediaType}
            controlMode={controlMode}
            formValues={formValues}
            disabled={disabled}
            loraOptions={loraOptions}
            imageFormatValue={imageFormatValue}
            onFieldChange={setFieldValue}
          />
        </div>

        <div className="d-flex justify-content-end">
          <Button type="submit" disabled={disabled || !canSubmit || isPromptEmpty}>
            {submitLabel}
          </Button>
        </div>
      </Stack>
    </Form>
  );
}
