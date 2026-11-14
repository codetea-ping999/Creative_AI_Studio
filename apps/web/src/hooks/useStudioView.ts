import { useCallback, useEffect, useState } from "react";

/**
 * Top-level screens of the studio. Each one owns a single task so the page
 * no longer stacks every panel into one long scroll.
 */
export type StudioView = "create" | "library" | "story" | "compare" | "setup";

export const studioViews: readonly StudioView[] = ["create", "library", "story", "compare", "setup"];

export const studioViewLabels: Record<StudioView, string> = {
  create: "つくる",
  library: "ライブラリ",
  story: "ストーリー",
  compare: "比較",
  setup: "準備",
};

export const studioViewDescriptions: Record<StudioView, string> = {
  create: "種類を選んで生成し、結果を確かめて直す",
  library: "作ったものを探して、詳しく見る・使う",
  story: "構想から絵コンテ、シーン素材、タイムラインまで",
  compare: "条件を振って一括生成し、並べて選ぶ",
  setup: "モデル、プロジェクト、表示の設定と利用実績",
};

const DEFAULT_VIEW: StudioView = "create";

export function viewHref(view: StudioView): string {
  return `#/${view}`;
}

/** Reads `#/story`, `#story` or `#/story/...`; anything else falls back to create. */
export function parseStudioViewHash(hash: string): StudioView {
  const segment = hash.replace(/^#\/?/, "").split(/[/?]/, 1)[0] ?? "";
  return (studioViews as readonly string[]).includes(segment)
    ? (segment as StudioView)
    : DEFAULT_VIEW;
}

function readWindowView(): StudioView {
  if (typeof window === "undefined") {
    return DEFAULT_VIEW;
  }
  return parseStudioViewHash(window.location.hash);
}

/**
 * Keeps the active screen in the URL hash so browser back/forward and
 * bookmarks work without a router dependency.
 */
export function useStudioView(): [StudioView, (view: StudioView) => void] {
  const [view, setViewState] = useState<StudioView>(readWindowView);

  useEffect(() => {
    const handleHashChange = () => setViewState(readWindowView());
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const setView = useCallback((next: StudioView) => {
    setViewState(next);
    const href = viewHref(next);
    if (window.location.hash !== href) {
      window.location.hash = href;
    }
    // jsdom lacks scrollIntoView; the optional call keeps tests quiet.
    document.querySelector(".studio-main")?.scrollIntoView?.({ block: "start" });
  }, []);

  return [view, setView];
}
