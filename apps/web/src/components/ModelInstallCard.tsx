import { useState } from "react";
import type {
  LocalModelInstallOptions,
  LocalModelInstallOutcome,
  ModelInstallGuide,
  ModelOption,
} from "./promptFormTypes";

type ModelInstallCardProps = {
  model: ModelOption;
  guide: ModelInstallGuide;
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

export function ModelInstallCard({ model, guide, onInstallLocalModel }: ModelInstallCardProps) {
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
    <div className="download-guide" aria-busy={isWorking}>
      <div className="download-guide__body">
        <strong>{model.displayName}</strong>
        <p>{guide.note}</p>
        {model.installPath ? (
          <p className="download-guide__path">
            配置先: <code>{model.installPath}</code>
          </p>
        ) : null}
      </div>
      <div className="download-guide__actions">
        <a href={guide.url} target="_blank" rel="noreferrer">
          Download
        </a>
        {canInstall ? (
          <button
            type="button"
            className="secondary-button"
            disabled={isWorking}
            onClick={() => {
              void run();
            }}
          >
            {isWorking ? "配置中…" : "手元のモデルを配置…"}
          </button>
        ) : null}
      </div>
      {isWorking ? (
        <p className="download-guide__status" role="status">
          コピーして確認しています。モデルが大きいと数分かかります。
        </p>
      ) : null}
      {state.kind === "done" ? (
        <p className="download-guide__status" role="status">
          ✓ {state.message}
        </p>
      ) : null}
      {state.kind === "failed" ? (
        <p className="download-guide__status download-guide__status--error" role="alert">
          ⚠ {state.message}
        </p>
      ) : null}
      {state.kind === "confirm" ? (
        <div className="download-guide__status" role="alert">
          <p>{state.message}</p>
          <div className="download-guide__actions">
            <button
              type="button"
              className="secondary-button"
              onClick={() => {
                void run({ sourcePath: state.sourcePath, replace: true });
              }}
            >
              置き換えて配置
            </button>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setState({ kind: "idle" })}
            >
              キャンセル
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
