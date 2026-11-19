/**
 * Creator rank: a playful level and title earned by finished generations.
 * Titles are original light-novel-style epithets (no existing work's names).
 * Keys are the Japanese source text; English lives in i18n/en.ts.
 */
export type CreatorRank = {
  level: number;
  title: string;
  /** Finished generations needed for the next level, or null at the top. */
  nextAt: number | null;
  /** Generations still needed for the next level (0 at the top). */
  remaining: number;
  /** 0..1 progress from this level's threshold to the next. */
  progress: number;
};

export const creatorRanks: ReadonlyArray<{ minimum: number; title: string }> = [
  { minimum: 0, title: "見習いクリエイター" },
  { minimum: 3, title: "駆け出しの演出家" },
  { minimum: 10, title: "術式を編む者" },
  { minimum: 25, title: "夕焼けの絵師" },
  { minimum: 50, title: "工房の主" },
  { minimum: 100, title: "物語を統べる者" },
  { minimum: 200, title: "伝説の創造主" },
];

export function creatorRank(finished: number): CreatorRank {
  const count = Math.max(0, Math.floor(Number.isFinite(finished) ? finished : 0));
  let index = 0;
  for (let i = 0; i < creatorRanks.length; i += 1) {
    if (count >= creatorRanks[i].minimum) {
      index = i;
    }
  }
  const current = creatorRanks[index];
  const next = creatorRanks[index + 1];
  if (!next) {
    return { level: index + 1, title: current.title, nextAt: null, remaining: 0, progress: 1 };
  }
  return {
    level: index + 1,
    title: current.title,
    nextAt: next.minimum,
    remaining: next.minimum - count,
    progress: (count - current.minimum) / (next.minimum - current.minimum),
  };
}
