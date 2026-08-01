import type { PromptFormSubmitValues } from "../components/promptFormTypes";

function buildGenerationPayload(
  values: PromptFormSubmitValues,
  projectId: string | null,
): Record<string, unknown> {
  const common = {
    prompt: values.prompt,
    model_id: values.modelId,
    seed: values.seed,
    project_id: projectId,
  };

  if (values.mediaType === "image") {
    return {
      ...common,
      negative_prompt: values.negativePrompt || null,
      output_format: values.outputFormat || "png",
      params: {
        width: values.width,
        height: values.height,
        steps: values.steps,
        guidance_scale: values.guidanceScale,
        variation_count: values.variationCount,
        ...(values.loraPath
          ? { lora_path: values.loraPath, lora_scale: values.loraScale }
          : {}),
      },
    };
  }

  if (values.mediaType === "audio") {
    return {
      ...common,
      output_format: values.outputFormat || "wav",
      params: {
        duration_seconds: values.durationSeconds,
        ...(values.extendStrideSeconds === null
          ? {}
          : { extend_stride_seconds: values.extendStrideSeconds }),
        guidance_scale: values.guidanceScale,
        bpm: values.bpm,
        mood: values.mood,
        genre: values.genre,
        instruments: values.instruments,
        structure: values.structure,
        temperature: values.temperature,
        top_k: values.topK,
        top_p: values.topP,
      },
    };
  }

  return {
    ...common,
    negative_prompt: values.negativePrompt || null,
    output_format: values.outputFormat || "gif",
    params: {
      width: values.width,
      height: values.height,
      duration_seconds: values.durationSeconds,
      ...(values.outputFormat === "mp4" ? { num_frames: 49, fps: 8 } : {}),
      camera_motion: values.cameraMotion,
      visual_style: values.visualStyle,
    },
  };
}

export function buildGeneratePayload(
  values: PromptFormSubmitValues,
  projectId: string | null,
): Record<string, unknown> {
  return buildGenerationPayload(values, projectId);
}

export function buildReusePayload(
  values: PromptFormSubmitValues,
  projectId: string | null,
  options: {
    action?: "variation" | "rerun" | "melody";
    params?: Record<string, unknown>;
  } = {},
): Record<string, unknown> {
  const generationPayload = buildGenerationPayload(values, projectId);
  const generationParams = generationPayload.params;
  return {
    action: options.action ?? "variation",
    ...generationPayload,
    params: {
      ...(generationParams && typeof generationParams === "object"
        ? (generationParams as Record<string, unknown>)
        : {}),
      ...options.params,
    },
  };
}
