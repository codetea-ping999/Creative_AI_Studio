import type { MediaType, ModelOption } from "./PromptForm";
import { getInstallGuide } from "../lib/modelInstallGuide";
import {
  formatPercent,
  formatScore,
  type GalleryStatsResponse,
  type MetricsSummaryResponse,
} from "../studio";
import { mediaKindLabels, type MediaReadiness } from "../lib/mediaReadiness";

type SetupPanelProps = {
  modelsByMedia: Record<MediaType, ModelOption[]>;
  readinessByMedia: Record<MediaType, MediaReadiness>;
  metrics: MetricsSummaryResponse | null;
  galleryStats: GalleryStatsResponse | null;
  onRetry: () => void;
  isRetrying: boolean;
};

const setupOrder: MediaType[] = ["video", "image", "audio"];

const readinessText: Record<MediaReadiness, string> = {
  loading: "確認中",
  ready: "使える",
  missing: "準備が必要",
  error: "確認できません",
};

const readinessMark: Record<MediaReadiness, string> = {
  loading: "…",
  ready: "●",
  missing: "○",
  error: "!",
};

/**
 * One place that answers "what can this machine make right now?" and, for
 * anything missing, where its model goes. The create screen links here
 * instead of repeating file lists inside the composer.
 */
export function SetupPanel({
  modelsByMedia,
  readinessByMedia,
  metrics,
  galleryStats,
  onRetry,
  isRetrying,
}: SetupPanelProps) {
  return (
    <>
      <section className="section-card" aria-labelledby="setup-models-heading">
        <div className="section-card__header">
          <div>
            <p className="eyebrow">モデル</p>
            <h2 id="setup-models-heading">この端末で使えるもの</h2>
          </div>
          <button
            type="button"
            className="secondary-button"
            onClick={onRetry}
            disabled={isRetrying}
          >
            {isRetrying ? "確認中..." : "再確認"}
          </button>
        </div>
        <ul className="setup-list">
          {setupOrder.map((media) => {
            const readiness = readinessByMedia[media];
            const models = modelsByMedia[media];
            const available = models.filter((model) => model.isAvailable);
            const missing = models.filter((model) => !model.isAvailable);
            return (
              <li key={media} className="setup-row">
                <div className="setup-row__head">
                  <strong>{mediaKindLabels[media]}</strong>
                  <span className={`setup-status setup-status--${readiness}`}>
                    <span aria-hidden="true">{readinessMark[readiness]}</span>{" "}
                    {readinessText[readiness]}
                  </span>
                  <span className="setup-row__summary">
                    {readiness === "ready"
                      ? available.map((model) => model.displayName).join("、")
                      : readiness === "missing"
                        ? models.length > 0
                          ? `${models.length} 件のモデルがすべて未配置です`
                          : "モデルが登録されていません"
                        : readiness === "error"
                          ? "モデル一覧を読み込めませんでした"
                          : ""}
                  </span>
                </div>
                {missing.length > 0 ? (
                  <details className="setup-row__details">
                    <summary>
                      未配置のモデル {missing.length} 件と配置先
                    </summary>
                    <ul className="setup-missing">
                      {missing.map((model) => {
                        const guide = getInstallGuide(media, model);
                        return (
                          <li key={model.id}>
                            <div>
                              <strong>{model.displayName}</strong>
                              {guide ? <p>{guide.note}</p> : null}
                              <p className="setup-missing__files">
                                {model.availabilityMessage ||
                                  "必要なモデルファイルが見つかりません。"}
                              </p>
                            </div>
                            {guide ? (
                              <a
                                className="secondary-button"
                                href={guide.url}
                                target="_blank"
                                rel="noreferrer"
                              >
                                取得先を開く
                              </a>
                            ) : null}
                          </li>
                        );
                      })}
                    </ul>
                  </details>
                ) : null}
              </li>
            );
          })}
        </ul>
        <p className="section-footnote">
          重みが無くても、文章と動画（絵コンテ）はすぐに作れます。画像と音楽は models/
          にモデルを置くと使えるようになります。
        </p>
      </section>

      <section className="section-card" aria-labelledby="setup-usage-heading">
        <div className="section-card__header">
          <div>
            <p className="eyebrow">利用実績</p>
            <h2 id="setup-usage-heading">これまでの生成</h2>
          </div>
        </div>
        <dl className="setup-stats">
          <div>
            <dt>生成した数</dt>
            <dd>{metrics?.total_jobs ?? 0}</dd>
          </div>
          <div>
            <dt>成功率</dt>
            <dd>{formatPercent(metrics?.success_rate)}</dd>
          </div>
          <div>
            <dt>保存した成果物</dt>
            <dd>{galleryStats?.total_items ?? 0}</dd>
          </div>
          <div>
            <dt>再利用した回数</dt>
            <dd>{galleryStats?.total_reuse_count ?? 0}</dd>
          </div>
          <div>
            <dt>書き出した回数</dt>
            <dd>{galleryStats?.total_export_count ?? 0}</dd>
          </div>
          <div>
            <dt>平均品質スコア</dt>
            <dd>{formatScore(metrics?.average_quality_score)}</dd>
          </div>
        </dl>
      </section>
    </>
  );
}

export default SetupPanel;
