import type { MediaType } from "../studio";
import { createOutputUrl, isAudioAsset, isVideoAsset } from "../studio";

type StagePreviewProps = {
  mediaType: MediaType;
  outputPath: string | null;
  title: string;
  subtitle: string;
};

type OutputThumbnailProps = {
  mediaType: MediaType;
  outputPath: string | null;
};

export function StagePreview({ mediaType, outputPath, title, subtitle }: StagePreviewProps) {
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="stage-surface">
        <div className="empty-stage">
          <div>
            <h3>プレビューを表示できません</h3>
            <p>{outputPath ?? "API から出力先が返っていません。"}</p>
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
            <p className="eyebrow">音声プレビュー</p>
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

export function OutputThumbnail({ mediaType, outputPath }: OutputThumbnailProps) {
  const src = createOutputUrl(outputPath);

  if (!src) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">未設定</span>
      </div>
    );
  }

  if (mediaType === "audio" || isAudioAsset(outputPath)) {
    return (
      <div className="gallery-item__thumb is-audio">
        <span className="gallery-item__audio-badge">音声</span>
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
