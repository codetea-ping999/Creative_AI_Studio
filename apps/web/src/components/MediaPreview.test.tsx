import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OutputThumbnail, StagePreview } from "./MediaPreview";
import { isGifAsset } from "../studio";

// #441: Stable storyboard visuals are GIFs stored as video-media assets. <video> cannot
// play GIF, so the Gallery showed "cannot play media" instead of the generated visual.
const storyboardGif = "outputs/videos/storyboard_scene_1.gif";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Detached Image used to extract the still frame. jsdom never fetches images, so tests
// drive load/error by hand.
class FakeImage {
  static instances: FakeImage[] = [];
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = 4;
  naturalHeight = 3;
  src = "";
  constructor() {
    FakeImage.instances.push(this);
  }
}

function stubImage() {
  FakeImage.instances = [];
  vi.stubGlobal("Image", FakeImage);
}

type ObserverCallback = (entries: Array<{ isIntersecting: boolean }>) => void;

function stubIntersectionObserver() {
  const observers: Array<{ callback: ObserverCallback; options?: IntersectionObserverInit }> =
    [];
  class FakeIntersectionObserver {
    callback: ObserverCallback;
    options?: IntersectionObserverInit;
    constructor(callback: ObserverCallback, options?: IntersectionObserverInit) {
      this.callback = callback;
      this.options = options;
      observers.push(this);
    }
    observe = vi.fn();
    disconnect = vi.fn();
    unobserve = vi.fn();
  }
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  return observers;
}

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

    it("defers fetching the still frame until the thumbnail nears the viewport", () => {
      stubReducedMotion(true);
      stubImage();
      const observers = stubIntersectionObserver();
      render(<OutputThumbnail mediaType="video" outputPath={storyboardGif} />);

      expect(FakeImage.instances).toHaveLength(0);
      expect(observers).toHaveLength(1);
      expect(observers[0].options?.rootMargin).toBe("200px");

      act(() => observers[0].callback([{ isIntersecting: false }]));
      expect(FakeImage.instances).toHaveLength(0);

      act(() => observers[0].callback([{ isIntersecting: true }]));
      expect(FakeImage.instances).toHaveLength(1);
      expect(FakeImage.instances[0].src).toMatch(/storyboard_scene_1\.gif$/);
    });

    it("fetches the still frame immediately when IntersectionObserver is unavailable", () => {
      stubReducedMotion(true);
      stubImage();
      vi.stubGlobal("IntersectionObserver", undefined);
      render(<OutputThumbnail mediaType="video" outputPath={storyboardGif} />);

      expect(FakeImage.instances).toHaveLength(1);
      expect(FakeImage.instances[0].src).toMatch(/storyboard_scene_1\.gif$/);
    });

    it("shows an accessible error when the stage still frame fails to load", () => {
      stubReducedMotion(true);
      stubImage();
      vi.stubGlobal("IntersectionObserver", undefined);
      const { container } = render(
        <StagePreview
          mediaType="video"
          outputPath={storyboardGif}
          title="Scene 1 storyboard"
          subtitle="storyboard-video"
        />,
      );

      act(() => FakeImage.instances[0].onerror?.());

      expect(container.querySelector("canvas")).toBeNull();
      expect(screen.getByRole("alert").textContent).toContain("Preview frame unavailable");
      expect(screen.getByRole("button", { name: "Play animation" })).toBeTruthy();
    });

    it("shows a text fallback when a thumbnail still frame fails to load", () => {
      stubReducedMotion(true);
      stubImage();
      vi.stubGlobal("IntersectionObserver", undefined);
      const { container } = render(
        <OutputThumbnail mediaType="video" outputPath={storyboardGif} />,
      );

      act(() => FakeImage.instances[0].onerror?.());

      expect(container.querySelector("canvas")).toBeNull();
      expect(screen.getByText("No preview")).toBeTruthy();
    });

    it("treats GIF URLs with a query string or fragment as GIFs", () => {
      stubReducedMotion(true);
      const { container } = render(
        <OutputThumbnail
          mediaType="video"
          outputPath="outputs/videos/preview.gif?token=abc#frame"
        />,
      );

      expect(container.querySelector("img")).toBeNull();
      expect(container.querySelector("canvas")).not.toBeNull();
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

describe("isGifAsset", () => {
  it.each([
    "outputs/videos/preview.gif",
    "outputs/videos/preview.GIF",
    "outputs/videos/preview.gif?token=abc",
    "outputs/videos/preview.gif#t=1",
    "http://127.0.0.1:8000/outputs/preview.gif?token=abc&v=2#x",
  ])("detects %s", (value) => {
    expect(isGifAsset(value)).toBe(true);
  });

  it.each([
    "outputs/videos/preview.mp4?file=x.gif",
    "outputs/videos/preview.png#x.gif",
    "outputs/videos/gif",
    null,
    "",
  ])("rejects %s", (value) => {
    expect(isGifAsset(value)).toBe(false);
  });
});
