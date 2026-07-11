import { createOutputUrl } from "../studioClient";
import { isAudioAsset, isVideoAsset } from "../studio";
import type { MediaType } from "./promptFormTypes";

type StagePreviewProps = {
  mediaType: MediaType;
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

  if (isVideoAsset(outputPath)) {
    return (
      <div className="stage-surface stage-surface--hero">
        <video controls muted playsInline preload="metadata" src={src} />
      </div>
    );
  }

  return (
    <div className="stage-surface stage-surface--hero">
      <img src={src} alt={title} loading="lazy" />
    </div>
  );
}

type OutputThumbnailProps = {
  mediaType: MediaType;
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

  if (isVideoAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb">
        <video muted playsInline preload="metadata" src={src} />
      </div>
    );
  }

  return (
    <div className="gallery-item__thumb">
      <img src={src} alt="" loading="lazy" />
    </div>
  );
}
