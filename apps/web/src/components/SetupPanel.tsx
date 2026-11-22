import type { MediaType, ModelOption } from "./PromptForm";
import { ModelInstallCard } from "./ModelInstallCard";
import type { LocalModelInstallOptions, LocalModelInstallOutcome } from "./promptFormTypes";
import { getInstallGuide } from "../lib/modelInstallGuide";
import {
  formatPercent,
  formatScore,
  type GalleryStatsResponse,
  type MetricsSummaryResponse,
} from "../studio";
import { mediaKindLabels, type MediaReadiness } from "../lib/mediaReadiness";
import { useT } from "../i18n";

type SetupPanelProps = {
  modelsByMedia: Record<MediaType, ModelOption[]>;
  readinessByMedia: Record<MediaType, MediaReadiness>;
  metrics: MetricsSummaryResponse | null;
  galleryStats: GalleryStatsResponse | null;
  onRetry: () => void;
  isRetrying: boolean;
  onInstallLocalModel?: (
    media: MediaType,
    modelId: string,
    options?: LocalModelInstallOptions,
  ) => Promise<LocalModelInstallOutcome>;
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
  onInstallLocalModel,
}: SetupPanelProps) {
  const t = useT();
  return (
    <>
      <section className="section-card" aria-labelledby="setup-models-heading">
        <div className="section-card__header">
          <div>
            <p className="eyebrow">{t("モデル")}</p>
            <h2 id="setup-models-heading">{t("この端末で使えるもの")}</h2>
          </div>
          <button
            type="button"
            className="secondary-button"
            onClick={onRetry}
            disabled={isRetrying}
          >
            {isRetrying ? t("確認中...") : t("再確認")}
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
                  <strong>{t(mediaKindLabels[media])}</strong>
                  <span className={`setup-status setup-status--${readiness}`}>
                    <span aria-hidden="true">{readinessMark[readiness]}</span>{" "}
                    {t(readinessText[readiness])}
                  </span>
                  <span className="setup-row__summary">
                    {readiness === "ready"
                      ? available.map((model) => model.displayName).join(t("、"))
                      : readiness === "missing"
                        ? models.length > 0
                          ? t("{count} 件のモデルがすべて未配置です", { count: models.length })
                          : t("モデルが登録されていません")
                        : readiness === "error"
                          ? t("モデル一覧を読み込めませんでした")
                          : ""}
                  </span>
                </div>
                {missing.length > 0 ? (
                  <details className="setup-row__details">
                    <summary>
                      {t("未配置のモデル {count} 件と配置先", { count: missing.length })}
                    </summary>
                    <ul className="setup-missing">
                      {missing.map((model) => (
                        <ModelInstallCard
                          key={model.id}
                          model={model}
                          guide={getInstallGuide(media, model)}
                          onInstallLocalModel={
                            onInstallLocalModel
                              ? (modelId, options) => onInstallLocalModel(media, modelId, options)
                              : undefined
                          }
                        />
                      ))}
                    </ul>
                  </details>
                ) : null}
              </li>
            );
          })}
        </ul>
        <p className="section-footnote">
          {t("重みが無くても、文章と動画（絵コンテ）はすぐに作れます。画像と音楽は models/ にモデルを置くと使えるようになります。")}
        </p>
      </section>

      <section className="section-card" aria-labelledby="setup-usage-heading">
        <div className="section-card__header">
          <div>
            <p className="eyebrow">{t("利用実績")}</p>
            <h2 id="setup-usage-heading">{t("これまでの生成")}</h2>
          </div>
        </div>
        <dl className="setup-stats">
          <div>
            <dt>{t("生成した数")}</dt>
            <dd>{metrics?.total_jobs ?? 0}</dd>
          </div>
          <div>
            <dt>{t("成功率")}</dt>
            <dd>{formatPercent(metrics?.success_rate)}</dd>
          </div>
          <div>
            <dt>{t("保存した成果物")}</dt>
            <dd>{galleryStats?.total_items ?? 0}</dd>
          </div>
          <div>
            <dt>{t("再利用した回数")}</dt>
            <dd>{galleryStats?.total_reuse_count ?? 0}</dd>
          </div>
          <div>
            <dt>{t("書き出した回数")}</dt>
            <dd>{galleryStats?.total_export_count ?? 0}</dd>
          </div>
          <div>
            <dt>{t("平均品質スコア")}</dt>
            <dd>{formatScore(metrics?.average_quality_score)}</dd>
          </div>
        </dl>
      </section>
    </>
  );
}

export default SetupPanel;
