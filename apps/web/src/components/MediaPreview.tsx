import { useEffect, useRef, useState } from "react";
import { createOutputUrl } from "../studioClient";
import {
  isAudioAsset,
  isGifAsset,
  isPlayableVideoAsset,
  isTextAsset,
  type GalleryMediaType,
} from "../studio";
import { usePrefersReducedMotion } from "../hooks/usePrefersReducedMotion";
import { excerptFromMarkdown, useTextAssetContent } from "../lib/textAssetPreview";
import { renderMarkdownLite } from "../lib/markdownLite";

type StagePreviewProps = {
  mediaType: GalleryMediaType;
  outputPath: string | null;
  title: string;
  subtitle: string;
};

export function StagePreview({
  mediaType,
  outputPath,
  title,
  subtitle,
}: StagePreviewProps) {
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="stage-surface">
        <div className="empty-stage">
          <div>
            <h3>Preview unavailable</h3>
            <p>{outputPath ?? "No output path was returned by the API."}</p>
          </div>
        </div>
      </div>
    );
  }

  if (mediaType === "audio" || isAudioAsset(outputPath)) {
    return (
      <div className="stage-surface stage-surface--audio">
        <div className="audio-preview">
          <div className="audio-preview__header">
            <p className="eyebrow">Audio Preview</p>
            <strong>{title}</strong>
            <p className="sidebar-copy">{subtitle}</p>
          </div>
          <audio controls preload="metadata" src={src} />
        </div>
      </div>
    );
  }

  if (isPlayableVideoAsset(outputPath)) {
    return (
      <div className="stage-surface stage-surface--hero">
        <video controls muted playsInline preload="metadata" src={src} />
      </div>
    );
  }

  if (mediaType === "text" || isTextAsset(outputPath)) {
    return <TextStagePreview src={src} title={title} subtitle={subtitle} />;
  }

  if (isGifAsset(outputPath)) {
    return (
      <div className="stage-surface stage-surface--hero">
        <MotionSafeGif key={src} src={src} alt={title} allowPlayback />
      </div>
    );
  }

  return (
    <div className="stage-surface stage-surface--hero">
      <img src={src} alt={title} loading="lazy" />
    </div>
  );
}

/**
 * Renders an animated GIF, but under `prefers-reduced-motion: reduce` shows a
 * still first frame instead (#448). Storyboard GIFs have no separate still
 * preview, so the first frame is drawn to a canvas. `allowPlayback` adds an
 * explicit control to opt into the animation.
 */
function MotionSafeGif({
  src,
  alt,
  allowPlayback = false,
}: {
  src: string;
  alt: string;
  allowPlayback?: boolean;
}) {
  const prefersReducedMotion = usePrefersReducedMotion();
  const [isPlaying, setIsPlaying] = useState(false);

  if (!prefersReducedMotion) {
    return <img src={src} alt={alt} loading="lazy" />;
  }

  return (
    <>
      {isPlaying ? (
        <img src={src} alt={alt} loading="lazy" />
      ) : (
        <GifStillFrame src={src} alt={alt} />
      )}
      {allowPlayback ? (
        <button
          type="button"
          className="secondary-button stage-surface__motion-toggle"
          onClick={() => setIsPlaying((current) => !current)}
        >
          {isPlaying ? "Show still frame" : "Play animation"}
        </button>
      ) : null}
    </>
  );
}

function GifStillFrame({ src, alt }: { src: string; alt: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const image = new Image();
    image.onload = () => {
      const canvas = canvasRef.current;
      if (!canvas) {
        return;
      }
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      // drawImage uses the GIF's first (default) frame, never an animation frame.
      canvas.getContext("2d")?.drawImage(image, 0, 0);
    };
    image.src = src;
    return () => {
      image.onload = null;
    };
  }, [src]);

  return alt ? (
    <canvas ref={canvasRef} role="img" aria-label={alt} />
  ) : (
    <canvas ref={canvasRef} aria-hidden="true" />
  );
}

function TextStagePreview({
  src,
  title,
  subtitle,
}: {
  src: string;
  title: string;
  subtitle: string;
}) {
  const { content, isLoading } = useTextAssetContent(src);
  return (
    <div className="stage-surface stage-surface--text">
      <div className="text-preview">
        <div className="text-preview__header">
          <p className="eyebrow">Text Preview</p>
          <strong>{title}</strong>
          <p className="sidebar-copy">{subtitle}</p>
        </div>
        <div className="text-preview__body">
          {isLoading ? (
            <p className="markdown-lite__paragraph">Loading…</p>
          ) : content ? (
            renderMarkdownLite(content)
          ) : (
            <p className="markdown-lite__paragraph">Preview unavailable.</p>
          )}
        </div>
      </div>
    </div>
  );
}

type OutputThumbnailProps = {
  mediaType: GalleryMediaType;
  outputPath: string | null;
};

export function OutputThumbnail({ mediaType, outputPath }: OutputThumbnailProps) {
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">None</span>
      </div>
    );
  }

  if (mediaType === "audio" || isAudioAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">Audio</span>
      </div>
    );
  }

  if (isPlayableVideoAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb">
        <video muted playsInline preload="metadata" src={src} />
      </div>
    );
  }

  if (mediaType === "text" || isTextAsset(outputPath)) {
    return <TextThumbnail src={src} />;
  }

  if (isGifAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb">
        <MotionSafeGif key={src} src={src} alt="" />
      </div>
    );
  }

  return (
    <div className="gallery-item__thumb">
      <img src={src} alt="" loading="lazy" />
    </div>
  );
}

function TextThumbnail({ src }: { src: string }) {
  const { content, isLoading } = useTextAssetContent(src);
  return (
    <div className="gallery-item__thumb gallery-item__thumb--text">
      <p className="gallery-item__text-excerpt">
        {isLoading ? "Loading…" : content ? excerptFromMarkdown(content) : "No preview"}
      </p>
    </div>
  );
}
