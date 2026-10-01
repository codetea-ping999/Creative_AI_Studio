import { useState } from "react";
import { createOutputUrl } from "../studioClient";
import { isAudioAsset, isPlayableVideoAsset, isTextAsset, type GalleryMediaType } from "../studio";
import { excerptFromMarkdown, useTextAssetContent } from "../lib/textAssetPreview";
import { renderMarkdownLite } from "../lib/markdownLite";

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
    const poster = isPlayableVideoAsset(posterPath) ? null : createOutputUrl(posterPath);
    // Keyed on the source so a failed load does not stick to the next selection.
    return <VideoStagePreview key={src} src={src} poster={poster} title={title} />;
  }

  if (mediaType === "text" || isTextAsset(outputPath)) {
    return <TextStagePreview src={src} title={title} subtitle={subtitle} />;
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
              <h3>Video unavailable</h3>
              <p role="alert">The video could not be loaded.</p>
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
          The video could not be loaded. Showing its still frame instead.
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
