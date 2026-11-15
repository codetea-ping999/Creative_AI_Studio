import { describe, expect, it } from "vitest";
import { creatorRank } from "./creatorRank";

describe("creatorRank", () => {
  it("starts at level 1 with nothing made", () => {
    expect(creatorRank(0)).toMatchObject({ level: 1, title: "見習いクリエイター", nextAt: 3, remaining: 3, progress: 0 });
  });

  it("levels up exactly at each threshold", () => {
    expect(creatorRank(2).level).toBe(1);
    expect(creatorRank(3)).toMatchObject({ level: 2, remaining: 7 });
    expect(creatorRank(10).level).toBe(3);
  });

  it("stops at the top rank", () => {
    expect(creatorRank(999)).toMatchObject({ level: 7, nextAt: null, remaining: 0, progress: 1 });
  });

  it("treats bad input as zero", () => {
    expect(creatorRank(Number.NaN).level).toBe(1);
    expect(creatorRank(-4).level).toBe(1);
  });
});

describe("creator rank titles", () => {
  it("each have an English title", async () => {
    const { creatorRanks } = await import("./creatorRank");
    const { en } = await import("../i18n/en");
    expect(creatorRanks.filter((rank) => !(rank.title in en))).toEqual([]);
  });
});
