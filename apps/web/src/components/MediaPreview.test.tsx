import { cleanup, fireEvent, render } from "@testing-library/react";
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

  it("uses a still preview as the poster of a playable video", () => {
    const { container } = render(
      <StagePreview
        mediaType="video"
        outputPath="outputs/videos/assembly.mp4"
        posterPath="outputs/videos/assembly_preview.png"
        title="Assembly"
        subtitle="assembly"
      />,
    );

    const video = container.querySelector("video");
    expect(video?.getAttribute("src")).toMatch(/\/outputs\/videos\/assembly\.mp4$/);
    expect(video?.getAttribute("poster")).toMatch(/\/outputs\/videos\/assembly_preview\.png$/);
  });

  it("falls back to the still frame with an alert when the video fails to load", () => {
    const { container, getByRole } = render(
      <StagePreview
        mediaType="video"
        outputPath="outputs/videos/missing.mp4"
        posterPath="outputs/videos/missing_preview.png"
        title="Assembly"
        subtitle="assembly"
      />,
    );

    fireEvent.error(container.querySelector("video") as HTMLVideoElement);

    expect(container.querySelector("video")).toBeNull();
    expect(getByRole("img", { name: "Assembly" }).getAttribute("src")).toMatch(
      /\/outputs\/videos\/missing_preview\.png$/,
    );
    expect(getByRole("alert").textContent).toMatch(/could not be loaded/);

    // The still can be missing too; then explain rather than show a broken image.
    fireEvent.error(getByRole("img", { name: "Assembly" }));
    expect(container.querySelector("img")).toBeNull();
    expect(getByRole("heading", { name: "Video unavailable" })).toBeTruthy();
  });

  it("explains a failed video without a poster and recovers for the next source", () => {
    const { container, getByRole, queryByRole, rerender } = render(
      <StagePreview
        mediaType="video"
        outputPath="outputs/videos/missing.mp4"
        title="Assembly"
        subtitle="assembly"
      />,
    );

    fireEvent.error(container.querySelector("video") as HTMLVideoElement);
    expect(getByRole("heading", { name: "Video unavailable" })).toBeTruthy();
    expect(getByRole("alert")).toBeTruthy();

    rerender(
      <StagePreview
        mediaType="video"
        outputPath="outputs/videos/other.mp4"
        title="Other"
        subtitle="assembly"
      />,
    );
    expect(container.querySelector("video")).not.toBeNull();
    expect(queryByRole("alert")).toBeNull();
  });
});
