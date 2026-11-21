/**
 * Production image presets (#8, #133).
 *
 * This module is the single source of truth for the Portrait / Anime / Cinematic presets.
 * A preset is only an input convenience: applying one fills the visible Composer controls,
 * the user can change any of them, and the job records the values actually sent. Nothing
 * about the selected preset is stored on the job or asset, so reusing an asset never depends
 * on which preset is selected now (#138).
 *
 * Contract (see docs/image-presets.md):
 * - `id`               stable identifier, lowercase kebab-case
 * - `name`             Japanese UI label (translated through the i18n dictionary)
 * - `intent`           one-line description of what the preset is for
 * - `modelId`          public_id of the image manifest the preset was tuned for
 * - `promptAdditions`  style words appended to the user's subject
 * - `negativePrompt`   full negative prompt (replaces the current one)
 * - `width`/`height`   pixels, multiples of 64 between 512 and 2048
 * - `steps`            1–150 inference steps
 * - `guidanceScale`    0–30 classifier-free guidance
 * - `loraScale`        optional, 0–2; only applied when the user has a LoRA selected
 *
 * The scheduler/sampler is not part of the contract: the Composer does not expose it and
 * each model keeps the scheduler its manifest/loader chooses.
 */

export type ImagePresetSpec = {
  id: string;
  name: string;
  intent: string;
  modelId: string;
  promptAdditions: string;
  negativePrompt: string;
  width: number;
  height: number;
  steps: number;
  guidanceScale: number;
  loraScale?: number;
};

export const IMAGE_PRESET_LIMITS = {
  minSize: 512,
  maxSize: 2048,
  sizeMultiple: 64,
  minSteps: 1,
  maxSteps: 150,
  minGuidance: 0,
  maxGuidance: 30,
  minLoraScale: 0,
  maxLoraScale: 2,
} as const;

/**
 * Returns a list of actionable problems with a preset; an empty list means it is valid.
 * `knownModelIds`, when given, rejects presets that point at a model with no manifest.
 */
export function validateImagePreset(
  preset: ImagePresetSpec,
  knownModelIds?: readonly string[],
): string[] {
  const errors: string[] = [];
  const label = preset.id || "(missing id)";
  const limits = IMAGE_PRESET_LIMITS;

  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(preset.id)) {
    errors.push(`${label}: id must be lowercase kebab-case (got "${preset.id}")`);
  }
  for (const field of ["name", "intent", "modelId", "promptAdditions"] as const) {
    if (!preset[field] || !preset[field].trim()) {
      errors.push(`${label}: ${field} must not be empty`);
    }
  }
  if (knownModelIds && !knownModelIds.includes(preset.modelId)) {
    errors.push(
      `${label}: modelId "${preset.modelId}" has no image manifest; use one of ${knownModelIds.join(", ")}`,
    );
  }
  for (const field of ["width", "height"] as const) {
    const value = preset[field];
    if (
      !Number.isInteger(value) ||
      value < limits.minSize ||
      value > limits.maxSize ||
      value % limits.sizeMultiple !== 0
    ) {
      errors.push(
        `${label}: ${field} must be a multiple of ${limits.sizeMultiple} between ${limits.minSize} and ${limits.maxSize} (got ${value})`,
      );
    }
  }
  if (
    !Number.isInteger(preset.steps) ||
    preset.steps < limits.minSteps ||
    preset.steps > limits.maxSteps
  ) {
    errors.push(
      `${label}: steps must be an integer between ${limits.minSteps} and ${limits.maxSteps} (got ${preset.steps})`,
    );
  }
  if (
    !Number.isFinite(preset.guidanceScale) ||
    preset.guidanceScale < limits.minGuidance ||
    preset.guidanceScale > limits.maxGuidance
  ) {
    errors.push(
      `${label}: guidanceScale must be between ${limits.minGuidance} and ${limits.maxGuidance} (got ${preset.guidanceScale})`,
    );
  }
  if (
    preset.loraScale !== undefined &&
    (!Number.isFinite(preset.loraScale) ||
      preset.loraScale < limits.minLoraScale ||
      preset.loraScale > limits.maxLoraScale)
  ) {
    errors.push(
      `${label}: loraScale must be between ${limits.minLoraScale} and ${limits.maxLoraScale} (got ${preset.loraScale})`,
    );
  }
  return errors;
}

/** Throws with every problem listed, so a bad preset fails loudly instead of being skipped. */
export function assertValidImagePresets(
  presets: readonly ImagePresetSpec[],
  knownModelIds?: readonly string[],
): void {
  const errors = presets.flatMap((preset) => validateImagePreset(preset, knownModelIds));
  const ids = presets.map((preset) => preset.id);
  const duplicates = ids.filter((id, index) => ids.indexOf(id) !== index);
  for (const id of new Set(duplicates)) {
    errors.push(`${id}: duplicate preset id`);
  }
  if (errors.length > 0) {
    throw new Error(`Invalid image preset:\n- ${errors.join("\n- ")}`);
  }
}

export const productionImagePresets: readonly ImagePresetSpec[] = [
  {
    id: "portrait",
    name: "ポートレート",
    intent: "One person, natural skin, shallow depth of field, vertical 4:5 framing.",
    modelId: "sdxl",
    promptAdditions:
      "portrait photograph, head and shoulders, soft key light, natural skin texture, 85mm lens, shallow depth of field, sharp focus on the eyes",
    negativePrompt:
      "deformed face, asymmetrical eyes, extra fingers, bad hands, plastic skin, oversaturated, harsh shadows, text, watermark, blurry, low quality",
    width: 832,
    height: 1024,
    steps: 30,
    guidanceScale: 6.5,
  },
  {
    id: "anime",
    name: "アニメ",
    intent: "Clean line art and flat cel shading on the anime SDXL model, square framing.",
    modelId: "anime-sdxl",
    promptAdditions:
      "anime style, clean line art, cel shading, vibrant colors, detailed eyes, expressive face, soft background",
    negativePrompt:
      "photorealistic, 3d render, bad anatomy, extra fingers, extra limbs, messy lines, muddy colors, text, watermark, blurry, low quality",
    width: 1024,
    height: 1024,
    steps: 28,
    guidanceScale: 6.5,
    loraScale: 0.8,
  },
  {
    id: "cinematic",
    name: "シネマティック",
    intent: "Wide 16:9 film still with dramatic lighting and color grading.",
    modelId: "sdxl",
    promptAdditions:
      "cinematic film still, wide shot, dramatic lighting, volumetric light, anamorphic lens, teal and orange color grading, film grain, highly detailed",
    negativePrompt:
      "flat lighting, overexposed, cartoon, bad anatomy, extra limbs, messy composition, text, watermark, subtitles, blurry, low quality",
    width: 1344,
    height: 768,
    steps: 32,
    guidanceScale: 7,
  },
];

/**
 * Appends a preset's style words to the user's subject. Any style words from another
 * production preset at the end of the prompt are removed first, so switching presets
 * does not stack them.
 */
export function applyPresetPrompt(currentPrompt: string, preset: ImagePresetSpec): string {
  let subject = currentPrompt.trim();
  for (const candidate of productionImagePresets) {
    const suffix = candidate.promptAdditions;
    if (subject === suffix) {
      subject = "";
    } else if (subject.endsWith(`, ${suffix}`)) {
      subject = subject.slice(0, -(suffix.length + 2)).trim();
    }
  }
  return subject ? `${subject}, ${preset.promptAdditions}` : preset.promptAdditions;
}
