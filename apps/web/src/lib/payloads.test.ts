import { describe, expect, it } from "vitest";

import type { PromptFormSubmitValues } from "../components/promptFormTypes";
import { buildGeneratePayload, buildReusePayload } from "./payloads";
import { buildQuickReviewPrompt, getQuickReviewIssueOptions } from "./quickReview";

const baseValues: PromptFormSubmitValues = {
  mediaType: "image",
  modelId: "sdxl",
  outputFormat: "png",
  prompt: "studio portrait",
  negativePrompt: "blurry",
  imageBriefPurpose: "SNS投稿",
  imageBriefSubject: "studio portrait",
  imageBriefMood: "やわらかい光",
  width: 1024,
  height: 1024,
  steps: 30,
  guidanceScale: 7.5,
  loraPath: "",
  loraScale: 0.8,
  seed: 42,
  durationSeconds: 8,
  bpm: 96,
  mood: "dreamy",
  cameraMotion: "push-in",
  visualStyle: "storyboard",
};

describe("generation payloads", () => {
  it("includes optional LoRA fields only when a LoRA is selected", () => {
    const withoutLora = buildGeneratePayload(baseValues, "project-1");
    expect(withoutLora).toMatchObject({
      model_id: "sdxl",
      project_id: "project-1",
      output_format: "png",
      params: { width: 1024, height: 1024, steps: 30 },
    });
    expect(withoutLora.params).not.toHaveProperty("lora_path");

    const withLora = buildGeneratePayload(
      { ...baseValues, loraPath: "models/loras/style.safetensors" },
      null,
    );
    expect(withLora.params).toMatchObject({
      lora_path: "models/loras/style.safetensors",
      lora_scale: 0.8,
    });
  });

  it("normalizes audio-specific parameters", () => {
    const payload = buildGeneratePayload(
      {
        ...baseValues,
        mediaType: "audio",
        modelId: "musicgen-small",
        outputFormat: "wav",
        durationSeconds: 12,
        guidanceScale: 3,
        bpm: 110,
        mood: "energetic",
      },
      null,
    );

    expect(payload).toEqual({
      prompt: "studio portrait",
      model_id: "musicgen-small",
      seed: 42,
      project_id: null,
      output_format: "wav",
      params: {
        duration_seconds: 12,
        guidance_scale: 3,
        bpm: 110,
        mood: "energetic",
      },
    });
  });

  it("adds reuse intent without changing the media payload", () => {
    expect(buildReusePayload(baseValues, "project-1")).toMatchObject({
      action: "variation",
      prompt: "studio portrait",
      project_id: "project-1",
      output_format: "png",
    });
  });

  it("keeps a quick-review action and tags with the reused request", () => {
    expect(
      buildReusePayload(baseValues, "project-1", {
        action: "rerun",
        params: {
          review_issue_tags: ["mood"],
          review_source: "quick-review",
        },
      }),
    ).toMatchObject({
      action: "rerun",
      params: {
        review_issue_tags: ["mood"],
        review_source: "quick-review",
      },
    });
  });

  it("offers only media-appropriate quick-review reasons and clear prompt instructions", () => {
    expect(getQuickReviewIssueOptions("image").map((option) => option.id)).toEqual([
      "composition",
      "subject_shape",
      "mood",
      "color_lighting",
      "remove_text",
    ]);
    expect(getQuickReviewIssueOptions("audio").map((option) => option.id)).toEqual([
      "mood",
      "duration_tempo",
    ]);
    expect(
      buildQuickReviewPrompt("editorial product shot", ["remove_text", "color_lighting"]),
    ).toBe(
      "editorial product shot Keep the main subject and intent. Improve the color and lighting. Remove all visible text.",
    );
  });

  it("uses the learned model MP4 contract for video generation", () => {
    const payload = buildGeneratePayload(
      {
        ...baseValues,
        mediaType: "video",
        modelId: "learned-video",
        outputFormat: "mp4",
        width: 720,
        height: 480,
      },
      null,
    );

    expect(payload).toMatchObject({
      model_id: "learned-video",
      output_format: "mp4",
      params: { width: 720, height: 480, num_frames: 49, fps: 8 },
    });
  });
});
