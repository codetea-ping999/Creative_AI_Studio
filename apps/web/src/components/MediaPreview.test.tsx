import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OutputThumbnail, StagePreview } from "./MediaPreview";

// #441: Stable storyboard visuals are GIFs stored as video-media assets. <video> cannot
// play GIF, so the Gallery showed "cannot play media" instead of the generated visual.
const storyboardGif = "outputs/videos/storyboard_scene_1.gif";

afterEach(() => {
  cleanup();
});

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
});
