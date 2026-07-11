import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PromptForm, type ModelOption } from "./components/PromptForm";
import { buildGeneratePayload, buildReusePayload } from "./lib/payloads";
import { isVideoAsset } from "./studio";
import { createOutputUrl, formatApiErrorDetail } from "./studioClient";

const storyboardModel: ModelOption = {
  id: "storyboard-video",
  displayName: "Storyboard Video",
  defaultParams: {},
  tags: ["video"],
  isAvailable: true,
  isDefault: true,
  runtimeStatus: "ready",
  availabilityMessage: "Ready",
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
          outputFormat: "gif",
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

  it("announces why a learned runtime is unavailable", () => {
    render(
      <PromptForm
        mediaType="video"
        modelOptions={[
          storyboardModel,
          {
            ...storyboardModel,
            id: "learned-video",
            displayName: "CogVideoX-2B",
            isAvailable: false,
            isDefault: false,
            runtimeStatus: "missing_files",
            availabilityMessage: "CogVideoX model_index.json is missing.",
          },
        ]}
        initialValues={{ modelId: "storyboard-video", outputFormat: "gif" }}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain(
      "CogVideoX-2B: CogVideoX model_index.json is missing.",
    );
    expect(
      (screen.getByRole("option", { name: /CogVideoX-2B/ }) as HTMLOptionElement)
        .disabled,
    ).toBe(true);
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
      outputFormat: "gif",
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
