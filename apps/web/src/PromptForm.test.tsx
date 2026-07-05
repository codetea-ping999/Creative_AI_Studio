import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
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
