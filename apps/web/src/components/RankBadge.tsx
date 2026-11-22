import { creatorRank } from "../lib/creatorRank";
import { useT } from "../i18n";

/** Creator level and title earned by finished generations. */
export function RankBadge({ finished }: { finished: number | null }) {
  const t = useT();
  if (finished === null) {
    return null;
  }
  const rank = creatorRank(finished);
  return (
    <div className="rank-badge">
      <div className="rank-badge__head">
        <span className="rank-badge__level">Lv {rank.level}</span>
        <strong className="rank-badge__title">{t(rank.title)}</strong>
      </div>
      <div
        className="rank-badge__bar"
        role="progressbar"
        aria-label={t("次の称号まで")}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(rank.progress * 100)}
      >
        <span style={{ width: `${Math.round(rank.progress * 100)}%` }} />
      </div>
      <p className="rank-badge__next">
        {rank.nextAt === null
          ? t("最高の称号に到達しました")
          : t("次の称号まであと {count} 本", { count: rank.remaining })}
      </p>
    </div>
  );
}
