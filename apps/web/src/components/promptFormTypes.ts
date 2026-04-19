export type MediaType = "image" | "audio" | "video";

export type ModelOption = {
  id: string;
  displayName: string;
  defaultParams: Record<string, unknown>;
  tags: string[];
  isAvailable: boolean;
  isDefault: boolean;
  availabilityReason?: string | null;
};

export type LoraOption = {
  id: string;
  displayName: string;
  path: string;
  relativePath: string;
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

export type ControlMode = "quick" | "advanced";

export type PromptFormProps = {
  formId?: string;
  mediaType?: MediaType;
  controlMode?: ControlMode;
  onControlModeChange?: (nextMode: ControlMode) => void;
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
