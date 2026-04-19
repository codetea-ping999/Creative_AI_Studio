import { defaultSubmitValues } from "../studio";
import type { MediaType, PromptFormSubmitValues } from "./promptFormTypes";

export type PromptFormState = {
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

function resolveDefaults(mediaType: MediaType): PromptFormSubmitValues {
  return defaultSubmitValues[mediaType];
}

export function createInitialState(
  mediaType: MediaType,
  initialValues?: Partial<PromptFormSubmitValues>,
): PromptFormState {
  const defaults = resolveDefaults(mediaType);
  const merged = { ...defaults, ...initialValues, mediaType };

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

export function parseRequiredInteger(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function parseRequiredFloat(value: string, fallback: number): number {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function parseOptionalInteger(value: string): number | null {
  if (value.trim() === "") {
    return null;
  }

  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

export function serializeDraft(
  mediaType: MediaType,
  formValues: PromptFormState,
): Partial<PromptFormSubmitValues> {
  const defaults = resolveDefaults(mediaType);

  return {
    mediaType,
    modelId: formValues.modelId,
    prompt: formValues.prompt,
    negativePrompt: formValues.negativePrompt,
    width: parseRequiredInteger(formValues.width, defaults.width),
    height: parseRequiredInteger(formValues.height, defaults.height),
    steps: parseRequiredInteger(formValues.steps, defaults.steps),
    guidanceScale: parseRequiredFloat(formValues.guidanceScale, defaults.guidanceScale),
    loraPath: formValues.loraPath.trim(),
    loraScale: parseRequiredFloat(formValues.loraScale, defaults.loraScale),
    seed: parseOptionalInteger(formValues.seed),
    durationSeconds: parseRequiredInteger(formValues.durationSeconds, defaults.durationSeconds),
    bpm: parseRequiredInteger(formValues.bpm, defaults.bpm),
    mood: formValues.mood,
    cameraMotion: formValues.cameraMotion,
    visualStyle: formValues.visualStyle,
  };
}

export function buildSubmitValues(
  mediaType: MediaType,
  formValues: PromptFormState,
): PromptFormSubmitValues {
  const defaults = resolveDefaults(mediaType);

  return {
    mediaType,
    modelId: formValues.modelId,
    prompt: formValues.prompt.trim(),
    negativePrompt: formValues.negativePrompt,
    width: parseRequiredInteger(formValues.width, defaults.width),
    height: parseRequiredInteger(formValues.height, defaults.height),
    steps: parseRequiredInteger(formValues.steps, defaults.steps),
    guidanceScale: parseRequiredFloat(formValues.guidanceScale, defaults.guidanceScale),
    loraPath: formValues.loraPath.trim(),
    loraScale: parseRequiredFloat(formValues.loraScale, defaults.loraScale),
    seed: parseOptionalInteger(formValues.seed),
    durationSeconds: parseRequiredInteger(formValues.durationSeconds, defaults.durationSeconds),
    bpm: parseRequiredInteger(formValues.bpm, defaults.bpm),
    mood: formValues.mood,
    cameraMotion: formValues.cameraMotion,
    visualStyle: formValues.visualStyle,
  };
}
