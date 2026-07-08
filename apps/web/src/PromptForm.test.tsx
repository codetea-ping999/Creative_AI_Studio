import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  buildGeneratePayload,
  buildReusePayload,
  createOutputUrl,
  formatApiErrorDetail,
  isVideoAsset,
} from "./App";
import { PromptForm, type ModelOption } from "./components/PromptForm";

const storyboardModel: ModelOption = {
  id: "storyboard-video",
  displayName: "Storyboard Video",
  defaultParams: {},
  tags: ["video"],
  isAvailable: true,
  isDefault: true,
};

describe("PromptForm", () => {
  it("submits normalized video composer values", async () => {
    const user = userEvent.setup();
    const handleSubmit = vi.fn();

    render(
      <PromptForm
        mediaType="video"
        modelOptions={[storyboardModel]}
        initialValues={{
          modelId: "storyboard-video",
          prompt: "verification storyboard",
          negativePrompt: "flat lighting",
          width: 320,
          height: 180,
          durationSeconds: 2,
          cameraMotion: "push-in",
          visualStyle: "storyboard",
        }}
        submitLabel="Create video"
        onSubmit={handleSubmit}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create video" }));

    expect(handleSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        mediaType: "video",
        modelId: "storyboard-video",
        prompt: "verification storyboard",
        negativePrompt: "flat lighting",
        width: 320,
        height: 180,
        durationSeconds: 2,
        cameraMotion: "push-in",
        visualStyle: "storyboard",
      }),
    );
  });
});

describe("studio helpers", () => {
  it("normalizes output paths served by the API static mount", () => {
    expect(createOutputUrl("outputs/images/sample.png")).toBe(
      "http://127.0.0.1:8000/outputs/images/sample.png",
    );
    expect(createOutputUrl("/repo/Creative_AI_Studio/outputs/videos/sample.gif")).toBe(
      "http://127.0.0.1:8000/outputs/videos/sample.gif",
    );
    expect(createOutputUrl("/Users/example/CreativeOutputs/images/sample.png")).toBe(
      "http://127.0.0.1:8000/outputs/images/sample.png",
    );
  });

  it("builds generate and reuse payloads with project binding", () => {
    const values = {
      mediaType: "video" as const,
      modelId: "storyboard-video",
      prompt: "verification storyboard",
      negativePrompt: "flat lighting",
      width: 320,
      height: 180,
      steps: 30,
      guidanceScale: 7.5,
      loraPath: "",
      loraScale: 0.8,
      seed: 123,
      durationSeconds: 2,
      bpm: 96,
      mood: "dreamy",
      cameraMotion: "push-in",
      visualStyle: "storyboard",
    };

    expect(buildGeneratePayload(values, "project_1")).toEqual({
      prompt: "verification storyboard",
      negative_prompt: "flat lighting",
      model_id: "storyboard-video",
      seed: 123,
      project_id: "project_1",
      output_format: "gif",
      params: {
        width: 320,
        height: 180,
        duration_seconds: 2,
        camera_motion: "push-in",
        visual_style: "storyboard",
      },
    });
    expect(buildReusePayload(values, null)).toMatchObject({
      action: "variation",
      project_id: null,
      output_format: "gif",
    });
  });

  it("recognizes gif and saved video assets", () => {
    expect(isVideoAsset("outputs/videos/board.gif")).toBe(true);
    expect(isVideoAsset("outputs/videos/render.mp4")).toBe(true);
    expect(isVideoAsset("outputs/images/still.png")).toBe(false);
  });

  it("formats FastAPI validation errors for display", () => {
    expect(
      formatApiErrorDetail([
        { loc: ["body", "prompt"], msg: "Field required", type: "missing" },
        { loc: ["body", "params", "width"], msg: "Input should be a valid integer" },
      ]),
    ).toBe(
      "body.prompt: Field required; body.params.width: Input should be a valid integer",
    );
  });
});
