import { useState } from "react";
import { useT } from "../i18n";
import type {
  LocalModelInstallOptions,
  LocalModelInstallOutcome,
  ModelInstallGuide,
  ModelOption,
} from "./promptFormTypes";

type ModelInstallCardProps = {
  model: ModelOption;
  guide: ModelInstallGuide | null;
  onInstallLocalModel?: (
    modelId: string,
    options?: LocalModelInstallOptions,
  ) => Promise<LocalModelInstallOutcome>;
};

type InstallState =
  | { kind: "idle" }
  | { kind: "working" }
  | { kind: "done"; message: string }
  | { kind: "failed"; message: string }
  | { kind: "confirm"; message: string; sourcePath: string };

/** One missing model on the Setup screen: where it goes, and a button to place it. */
export function ModelInstallCard({ model, guide, onInstallLocalModel }: ModelInstallCardProps) {
  const t = useT();
  const [state, setState] = useState<InstallState>({ kind: "idle" });
  const canInstall = Boolean(onInstallLocalModel) && model.supportsLocalInstall;
  const isWorking = state.kind === "working";

  async function run(options?: LocalModelInstallOptions): Promise<void> {
    if (!onInstallLocalModel) {
      return;
    }
    setState({ kind: "working" });
    const outcome = await onInstallLocalModel(model.id, options);
    if (outcome.status === "cancelled") {
      setState({ kind: "idle" });
    } else if (outcome.status === "installed") {
      setState({ kind: "done", message: outcome.message });
    } else if (outcome.status === "needs_replace") {
      setState({ kind: "confirm", message: outcome.message, sourcePath: outcome.sourcePath });
    } else {
      setState({ kind: "failed", message: outcome.message });
    }
  }

  return (
    <li className="setup-install" aria-busy={isWorking}>
      <div className="setup-install__row">
        <div>
          <strong>{model.displayName}</strong>
          {guide ? <p>{t(guide.note)}</p> : null}
          {model.installPath ? (
            <p className="setup-missing__files">
              {t("配置先")}: <code>{model.installPath}</code>
            </p>
          ) : null}
          <p className="setup-missing__files">
            {model.availabilityMessage || t("必要なモデルファイルが見つかりません。")}
          </p>
        </div>
        <div className="setup-install__actions">
          {guide ? (
            <a className="secondary-button" href={guide.url} target="_blank" rel="noreferrer">
              {t("取得先を開く")}
            </a>
          ) : null}
          {canInstall ? (
            <button
              type="button"
              className="secondary-button"
              disabled={isWorking}
              onClick={() => {
                void run();
              }}
            >
              {isWorking ? t("配置中…") : t("手元のモデルを配置…")}
            </button>
          ) : null}
        </div>
      </div>
      {isWorking ? (
        <p className="setup-install__status" role="status">
          {t("コピーして確認しています。モデルが大きいと数分かかります。")}
        </p>
      ) : null}
      {state.kind === "done" ? (
        <p className="setup-install__status" role="status">
          ✓ {state.message}
        </p>
      ) : null}
      {state.kind === "failed" ? (
        <p className="setup-install__status setup-install__status--error" role="alert">
          ⚠ {state.message}
        </p>
      ) : null}
      {state.kind === "confirm" ? (
        <div className="setup-install__status" role="alert">
          <p>{state.message}</p>
          <div className="setup-install__actions">
            <button
              type="button"
              className="secondary-button"
              onClick={() => {
                void run({ sourcePath: state.sourcePath, replace: true });
              }}
            >
              {t("置き換えて配置")}
            </button>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setState({ kind: "idle" })}
            >
              {t("キャンセル")}
            </button>
          </div>
        </div>
      ) : null}
    </li>
  );
}
