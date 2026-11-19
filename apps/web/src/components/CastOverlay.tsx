import { useEffect } from "react";
import { useT } from "../i18n";

export type CastCue = {
  /** Changes for every cue so the same kind can replay. */
  id: number;
  kind: "start" | "complete" | "levelup";
  level?: number;
  title?: string;
};

const CUE_DURATION_MS = 1400;

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  } catch {
    return false;
  }
}

/**
 * A short cut-in shown when a generation starts, finishes or raises the
 * creator level. Purely decorative: the same news is already in the status
 * text, so the overlay is hidden from assistive tech and never takes clicks.
 */
export function CastOverlay({ cue, onDone }: { cue: CastCue | null; onDone: () => void }) {
  const t = useT();

  useEffect(() => {
    if (!cue) {
      return;
    }
    const timer = window.setTimeout(onDone, CUE_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, [cue, onDone]);

  if (!cue || prefersReducedMotion()) {
    return null;
  }

  const word =
    cue.kind === "start" ? t("展開！") : cue.kind === "complete" ? t("完成！") : t("レベルアップ！");
  const sub =
    cue.kind === "start"
      ? "CAST"
      : cue.kind === "complete"
        ? "COMPLETE"
        : t("Lv {level}・{title}", { level: cue.level ?? 1, title: t(cue.title ?? "") });

  return (
    <div key={cue.id} className={`cast-overlay cast-overlay--${cue.kind}`} aria-hidden="true">
      <div className="cast-overlay__band" />
      <div className="cast-overlay__text">
        <strong className="cast-overlay__word">{word}</strong>
        <span className="cast-overlay__sub">{sub}</span>
      </div>
    </div>
  );
}
