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
import { useT } from "../i18n";

type StagePreviewProps = {
  mediaType: GalleryMediaType;
  outputPath: string | null;
  title: string;
  subtitle: string;
  /** Still frame shown before a playable video starts (e.g. an Assembly `*_preview.png`). */
  posterPath?: string | null;
};

export function StagePreview({
  mediaType,
  outputPath,
  title,
  subtitle,
  posterPath = null,
}: StagePreviewProps) {
  const t = useT();
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="stage-surface">
        <div className="empty-stage">
          <div>
            <h3>{t("プレビューを表示できません")}</h3>
            <p>{outputPath ?? t("出力ファイルの場所が返ってきませんでした。")}</p>
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
            <p className="eyebrow">{t("音声")}</p>
            <strong>{title}</strong>
            <p className="sidebar-copy">{subtitle}</p>
          </div>
          <audio controls preload="metadata" src={src} />
        </div>
      </div>
    );
  }

  if (isPlayableVideoAsset(outputPath)) {
    const poster = isPlayableVideoAsset(posterPath) ? null : createOutputUrl(posterPath);
    // Keyed on the source so a failed load does not stick to the next selection.
    return <VideoStagePreview key={src} src={src} poster={poster} title={title} />;
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

function VideoStagePreview({
  src,
  poster,
  title,
}: {
  src: string;
  poster: string | null;
  title: string;
}) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  const [posterFailed, setPosterFailed] = useState(false);

  // A missing or undecodable video leaves the browser on a blank frame with an
  // endless spinner and drops the poster, so fall back to the still with a
  // textual error instead.
  if (failed) {
    if (!poster || posterFailed) {
      return (
        <div className="stage-surface">
          <div className="empty-stage">
            <div>
              <h3>{t("動画を表示できません")}</h3>
              <p role="alert">{t("動画を読み込めませんでした。")}</p>
            </div>
          </div>
        </div>
      );
    }
    return (
      <>
        <div className="stage-surface stage-surface--hero">
          <img src={poster} alt={title} onError={() => setPosterFailed(true)} />
        </div>
        <p className="error-banner" role="alert">
          {t("動画を読み込めなかったため、静止画を表示しています。")}
        </p>
      </>
    );
  }

  return (
    <div className="stage-surface stage-surface--hero">
      <video
        controls
        muted
        playsInline
        preload="metadata"
        src={src}
        poster={poster ?? undefined}
        aria-label={title}
        onError={() => setFailed(true)}
      />
    </div>
  );
}

/**
 * Renders an animated GIF, but under `prefers-reduced-motion: reduce` shows a
 * still first frame instead (#448). Storyboard GIFs have no separate still
 * preview, so the first frame is drawn to a canvas. `allowPlayback` adds an
 * explicit control to opt into the animation.
 */
export function MotionSafeGif({
  src,
  alt,
  allowPlayback = false,
}: {
  src: string;
  alt: string;
  allowPlayback?: boolean;
}) {
  const t = useT();
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
        <GifStillFrame src={src} alt={alt} variant={allowPlayback ? "stage" : "thumbnail"} />
      )}
      {allowPlayback ? (
        <button
          type="button"
          className="secondary-button stage-surface__motion-toggle"
          onClick={() => setIsPlaying((current) => !current)}
        >
          {isPlaying ? t("止める") : t("再生する")}
        </button>
      ) : null}
    </>
  );
}

// Start fetching a still frame shortly before the thumbnail scrolls into view, matching
// the `loading="lazy"` behaviour of the animated <img> path.
const STILL_FRAME_ROOT_MARGIN = "200px";

function GifStillFrame({
  src,
  alt,
  variant,
}: {
  src: string;
  alt: string;
  variant: "stage" | "thumbnail";
}) {
  const t = useT();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [isNearViewport, setIsNearViewport] = useState(
    () => typeof IntersectionObserver === "undefined",
  );
  // MotionSafeGif is keyed by src, so a new src remounts this and resets the error.
  const [hasError, setHasError] = useState(false);

  useEffect(() => {
    if (isNearViewport) {
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer.disconnect();
          setIsNearViewport(true);
        }
      },
      { rootMargin: STILL_FRAME_ROOT_MARGIN },
    );
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [isNearViewport]);

  useEffect(() => {
    if (!isNearViewport) {
      return;
    }
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
    // 404s and decode failures would otherwise leave an empty canvas behind.
    image.onerror = () => setHasError(true);
    image.src = src;
    return () => {
      image.onload = null;
      image.onerror = null;
    };
  }, [isNearViewport, src]);

  if (hasError) {
    return variant === "stage" ? (
      <div className="media-still-error media-still-error--stage" role="alert">
        <span className="media-still-error__mark" aria-hidden="true">
          !
        </span>
        <div>
          <strong>{t("プレビューを表示できません")}</strong>
          <p>{t("GIF を読み込めませんでした。")}</p>
        </div>
      </div>
    ) : (
      <div className="media-still-error media-still-error--thumbnail">
        <span className="media-still-error__mark" aria-hidden="true">
          !
        </span>
        <span>{t("プレビューなし")}</span>
      </div>
    );
  }

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
  const t = useT();
  const { content, isLoading } = useTextAssetContent(src);
  return (
    <div className="stage-surface stage-surface--text">
      <div className="text-preview">
        <div className="text-preview__header">
          <p className="eyebrow">{t("文章")}</p>
          <strong>{title}</strong>
          <p className="sidebar-copy">{subtitle}</p>
        </div>
        <div className="text-preview__body">
          {isLoading ? (
            <p className="markdown-lite__paragraph">{t("読み込み中…")}</p>
          ) : content ? (
            renderMarkdownLite(content)
          ) : (
            <p className="markdown-lite__paragraph">{t("プレビューを表示できません。")}</p>
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
  const t = useT();
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">{t("なし")}</span>
      </div>
    );
  }

  if (mediaType === "audio" || isAudioAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">{t("音声")}</span>
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
  const t = useT();
  const { content, isLoading } = useTextAssetContent(src);
  return (
    <div className="gallery-item__thumb gallery-item__thumb--text">
      <p className="gallery-item__text-excerpt">
        {isLoading ? t("読み込み中…") : content ? excerptFromMarkdown(content) : t("プレビューなし")}
      </p>
    </div>
  );
}
