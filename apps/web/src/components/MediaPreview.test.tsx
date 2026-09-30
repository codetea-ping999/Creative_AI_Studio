import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OutputThumbnail, StagePreview } from "./MediaPreview";

// #441: Stable storyboard visuals are GIFs stored as video-media assets. <video> cannot
// play GIF, so the Gallery showed "cannot play media" instead of the generated visual.
const storyboardGif = "outputs/videos/storyboard_scene_1.gif";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubReducedMotion(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === "(prefers-reduced-motion: reduce)" ? matches : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

describe("MediaPreview", () => {
  it("renders a storyboard GIF thumbnail as an image", () => {
    const { container } = render(
      <OutputThumbnail mediaType="video" outputPath={storyboardGif} />,
    );

    expect(container.querySelector("video")).toBeNull();
    expect(container.querySelector("img")?.getAttribute("src")).toMatch(
      /\/outputs\/videos\/storyboard_scene_1\.gif$/,
    );
  });

  it("renders a storyboard GIF stage preview as an image with alt text", () => {
    const { container, getByRole } = render(
      <StagePreview
        mediaType="video"
        outputPath={storyboardGif}
        title="Scene 1 storyboard"
        subtitle="storyboard-video"
      />,
    );

    expect(container.querySelector("video")).toBeNull();
    expect(getByRole("img", { name: "Scene 1 storyboard" })).toBeTruthy();
  });

  it("keeps MP4 assets in a video player", () => {
    const { container } = render(
      <StagePreview
        mediaType="video"
        outputPath="outputs/videos/assembly.mp4"
        title="Assembly"
        subtitle="assembly"
      />,
    );

    expect(container.querySelector("video")).not.toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  // #448: storyboard GIFs kept animating for users who asked for reduced motion.
  describe("under prefers-reduced-motion: reduce", () => {
    it("shows a still frame instead of an autoplaying GIF thumbnail", () => {
      stubReducedMotion(true);
      const { container } = render(
        <OutputThumbnail mediaType="video" outputPath={storyboardGif} />,
      );

      expect(container.querySelector("img")).toBeNull();
      expect(container.querySelector("canvas")?.getAttribute("aria-hidden")).toBe("true");
    });

    it("shows a still stage frame until the user plays the animation", async () => {
      stubReducedMotion(true);
      const user = userEvent.setup();
      const { container } = render(
        <StagePreview
          mediaType="video"
          outputPath={storyboardGif}
          title="Scene 1 storyboard"
          subtitle="storyboard-video"
        />,
      );

      expect(container.querySelector("img")).toBeNull();
      expect(screen.getByRole("img", { name: "Scene 1 storyboard" }).tagName).toBe("CANVAS");

      await user.click(screen.getByRole("button", { name: "Play animation" }));
      expect(container.querySelector("img")?.getAttribute("src")).toMatch(
        /storyboard_scene_1\.gif$/,
      );

      await user.click(screen.getByRole("button", { name: "Show still frame" }));
      expect(container.querySelector("img")).toBeNull();
    });

    it("leaves still images unchanged", () => {
      stubReducedMotion(true);
      const { container } = render(
        <OutputThumbnail mediaType="image" outputPath="outputs/images/still.png" />,
      );

      expect(container.querySelector("img")?.getAttribute("src")).toMatch(/still\.png$/);
      expect(container.querySelector("canvas")).toBeNull();
    });
  });

  it("keeps animating GIFs when reduced motion is not requested", () => {
    stubReducedMotion(false);
    const { container } = render(
      <StagePreview
        mediaType="video"
        outputPath={storyboardGif}
        title="Scene 1 storyboard"
        subtitle="storyboard-video"
      />,
    );

    expect(container.querySelector("img")?.getAttribute("src")).toMatch(/storyboard_scene_1\.gif$/);
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.queryByRole("button", { name: "Play animation" })).toBeNull();
  });
});
