export type MediaType = "image" | "audio" | "video";

export type ModelOption = {
  id: string;
  displayName: string;
  defaultParams: Record<string, unknown>;
  tags: string[];
  isAvailable: boolean;
  isDefault: boolean;
  runtimeStatus: string;
  availabilityMessage: string;
};

export type LoraOption = {
  id: string;
  displayName: string;
  path: string;
  relativePath: string;
};

export type PromptFormState = {
  modelId: string;
  outputFormat: string;
  prompt: string;
  negativePrompt: string;
  imageBriefPurpose: string;
  imageBriefSubject: string;
  imageBriefMood: string;
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
  outputFormat: string;
  prompt: string;
  negativePrompt: string;
  imageBriefPurpose: string;
  imageBriefSubject: string;
  imageBriefMood: string;
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

export type ControlMode = "quick" | "advanced";

export type ModelInstallGuide = {
  label: string;
  url: string;
  note: string;
};
