import type { ReactNode } from "react";
import Badge from "react-bootstrap/Badge";
import Button from "react-bootstrap/Button";
import Col from "react-bootstrap/Col";
import Form from "react-bootstrap/Form";
import Row from "react-bootstrap/Row";
import Stack from "react-bootstrap/Stack";
import { imageFormatPresets, type ModelInstallGuide } from "./promptFormConfig";
import type {
  ControlMode,
  LoraOption,
  MediaType,
  ModelOption,
} from "./promptFormTypes";
import type { PromptFormState } from "./promptFormState";

export type MissingModelGuide = {
  modelId: string;
  displayName: string;
  guide: ModelInstallGuide;
};

type SetPromptFormField = <K extends keyof PromptFormState>(
  field: K,
  value: PromptFormState[K],
) => void;

type PromptFormModelSectionProps = {
  mediaType: MediaType;
  controlMode: ControlMode;
  modelOptions: ModelOption[];
  loraOptions: LoraOption[];
  formValues: PromptFormState;
  disabled: boolean;
  availableModelCount: number;
  unavailableModelCount: number;
  missingModelGuides: MissingModelGuide[];
  onModelChange: (modelId: string) => void;
  onFieldChange: SetPromptFormField;
};

type PromptFormPromptSectionProps = {
  mediaType: MediaType;
  controlMode: ControlMode;
  formValues: PromptFormState;
  disabled: boolean;
  isPromptEmpty: boolean;
  promptFieldId?: string;
  promptHelpId?: string;
  promptHelpText: string;
  onFieldChange: SetPromptFormField;
};

type PromptFormControlsSectionProps = {
  mediaType: MediaType;
  controlMode: ControlMode;
  formValues: PromptFormState;
  disabled: boolean;
  loraOptions: LoraOption[];
  imageFormatValue: string;
  onFieldChange: SetPromptFormField;
};

type QuickControlsProps = {
  mediaType: MediaType;
  formValues: PromptFormState;
  disabled: boolean;
  loraOptions: LoraOption[];
  imageFormatValue: string;
  onFieldChange: SetPromptFormField;
};

type AdvancedControlsProps = {
  mediaType: MediaType;
  formValues: PromptFormState;
  disabled: boolean;
  onFieldChange: SetPromptFormField;
};

type PanelFrameProps = {
  eyebrow: string;
  title: string;
  aside?: ReactNode;
  children: ReactNode;
};

function PanelFrame({ eyebrow, title, aside, children }: PanelFrameProps) {
  return (
    <section className="form-panel">
      <div className="form-panel__header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h3 className="form-panel__title">{title}</h3>
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function PromptFormModelSection({
  mediaType,
  controlMode,
  modelOptions,
  loraOptions,
  formValues,
  disabled,
  availableModelCount,
  unavailableModelCount,
  missingModelGuides,
  onModelChange,
  onFieldChange,
}: PromptFormModelSectionProps) {
  const selectedModel = modelOptions.find((option) => option.id === formValues.modelId) ?? null;
  const title =
    mediaType === "image"
      ? "モデルとスタイルのベースを選ぶ"
      : mediaType === "audio"
        ? "モデルと音のベースを選ぶ"
        : "モデルと動画のベースを選ぶ";

  return (
    <PanelFrame
      eyebrow="モデル設定"
      title={title}
      aside={
        modelOptions.length > 0 ? (
          <div className="d-flex flex-wrap justify-content-end gap-2">
            <Badge bg="secondary">利用可能 {availableModelCount}/{modelOptions.length}</Badge>
            {unavailableModelCount > 0 ? (
              <Badge bg="secondary">未導入 {unavailableModelCount}</Badge>
            ) : null}
          </div>
        ) : null
      }
    >
      <Stack gap={3}>
        <Row className="g-3">
          {modelOptions.length > 0 ? (
            <Col xs={12}>
              <Form.Group>
                <Form.Label>モデル</Form.Label>
                <Form.Select
                  name="modelId"
                  value={formValues.modelId}
                  onChange={(event) => onModelChange(event.target.value)}
                  disabled={disabled}
                >
                  {modelOptions.map((option) => (
                    <option key={option.id} value={option.id} disabled={!option.isAvailable}>
                      {option.displayName}
                      {option.isAvailable ? "" : "（未導入）"}
                    </option>
                  ))}
                </Form.Select>
              </Form.Group>
            </Col>
          ) : null}

          {controlMode === "advanced" && mediaType === "image" && loraOptions.length > 0 ? (
            <Col xs={12}>
              <Form.Group>
                <Form.Label>LoRA カタログ</Form.Label>
                <Form.Select
                  name="loraPreset"
                  value={
                    loraOptions.some((option) => option.path === formValues.loraPath)
                      ? formValues.loraPath
                      : ""
                  }
                  onChange={(event) => onFieldChange("loraPath", event.target.value)}
                  disabled={disabled}
                >
                  <option value="">なし</option>
                  {loraOptions.map((option) => (
                    <option key={option.id} value={option.path}>
                      {option.displayName} ({option.relativePath})
                    </option>
                  ))}
                </Form.Select>
                <Form.Text className="field-help">
                  ローカル配置済み LoRA を選択できます。必要なら下で手入力も可能です。
                </Form.Text>
              </Form.Group>
            </Col>
          ) : null}
        </Row>

        {selectedModel?.availabilityReason ? (
          <Form.Text className="field-help">
            利用条件: {selectedModel.availabilityReason}
          </Form.Text>
        ) : null}

        {missingModelGuides.length > 0 ? (
          <div className="download-guide-list" aria-label="Model download guides">
            {missingModelGuides.map((item) => (
              <div key={item.modelId} className="download-guide">
                <div>
                  <strong>{item.displayName}</strong>
                  <p>
                    {item.guide.note}
                    {modelOptions.find((option) => option.id === item.modelId)?.availabilityReason
                      ? ` / ${modelOptions.find((option) => option.id === item.modelId)?.availabilityReason}`
                      : ""}
                  </p>
                </div>
                <Button
                  as="a"
                  href={item.guide.url}
                  target="_blank"
                  rel="noreferrer"
                  variant="outline-primary"
                  size="sm"
                >
                  ダウンロード
                </Button>
              </div>
            ))}
          </div>
        ) : null}
      </Stack>
    </PanelFrame>
  );
}

export function PromptFormPromptSection({
  mediaType,
  controlMode,
  formValues,
  disabled,
  isPromptEmpty,
  promptFieldId,
  promptHelpId,
  promptHelpText,
  onFieldChange,
}: PromptFormPromptSectionProps) {
  const title =
    mediaType === "image"
      ? "まずは主題を決める"
      : mediaType === "audio"
        ? "まずは雰囲気を決める"
        : "まずは見せたい動きを決める";

  return (
    <PanelFrame
      eyebrow="Prompt"
      title={title}
      aside={<span className="info-chip">{controlMode === "quick" ? "おすすめ" : "詳細"}</span>}
    >
      <Stack gap={3}>
        <Form.Group>
          <Form.Label>内容</Form.Label>
          <Form.Control
            as="textarea"
            id={promptFieldId}
            name="prompt"
            rows={controlMode === "quick" ? 4 : 5}
            placeholder={
              mediaType === "image"
                ? "生成したい画像の内容を日本語で入力"
                : mediaType === "audio"
                  ? "作りたい音の雰囲気や用途を入力"
                  : "作りたい動画の構図や動きを入力"
            }
            value={formValues.prompt}
            onChange={(event) => onFieldChange("prompt", event.target.value)}
            disabled={disabled}
            aria-invalid={isPromptEmpty}
            aria-describedby={promptHelpId}
          />
          <Form.Text id={promptHelpId} className={isPromptEmpty ? "text-danger" : "field-help"}>
            {promptHelpText}
          </Form.Text>
        </Form.Group>

        {(controlMode === "advanced" || mediaType === "video") &&
        (mediaType === "image" || mediaType === "video") ? (
          <Form.Group>
            <Form.Label>除外したい要素</Form.Label>
            <Form.Control
              as="textarea"
              name="negativePrompt"
              rows={4}
              placeholder="入れたくない要素や避けたい表現を入力"
              value={formValues.negativePrompt}
              onChange={(event) => onFieldChange("negativePrompt", event.target.value)}
              disabled={disabled}
            />
          </Form.Group>
        ) : null}
      </Stack>
    </PanelFrame>
  );
}

export function PromptFormControlsSection({
  mediaType,
  controlMode,
  formValues,
  disabled,
  loraOptions,
  imageFormatValue,
  onFieldChange,
}: PromptFormControlsSectionProps) {
  if (controlMode === "quick") {
    return renderQuickControls({
      mediaType,
      formValues,
      disabled,
      loraOptions,
      imageFormatValue,
      onFieldChange,
    });
  }

  return renderAdvancedControls({
    mediaType,
    formValues,
    disabled,
    onFieldChange,
  });
}

function renderQuickControls({
  mediaType,
  formValues,
  disabled,
  loraOptions,
  imageFormatValue,
  onFieldChange,
}: QuickControlsProps) {
  if (mediaType === "image") {
    return (
      <PanelFrame eyebrow="おすすめ設定" title="まずはサイズと雰囲気だけ決める">
        <Row className="g-3">
          <Col md={6}>
            <Form.Group>
              <Form.Label>サイズ</Form.Label>
              <Form.Select
                name="imageFormat"
                value={imageFormatValue}
                onChange={(event) => {
                  const nextPreset = imageFormatPresets.find(
                    (preset) => preset.value === event.target.value,
                  );
                  if (!nextPreset) {
                    return;
                  }
                  onFieldChange("width", String(nextPreset.width));
                  onFieldChange("height", String(nextPreset.height));
                }}
                disabled={disabled}
              >
                {imageFormatPresets.map((preset) => (
                  <option key={preset.value} value={preset.value}>
                    {preset.label}
                  </option>
                ))}
                <option value="custom">カスタム</option>
              </Form.Select>
            </Form.Group>
          </Col>

          {loraOptions.length > 0 ? (
            <Col md={6}>
              <Form.Group>
                <Form.Label>スタイル</Form.Label>
                <Form.Select
                  name="loraPreset"
                  value={
                    loraOptions.some((option) => option.path === formValues.loraPath)
                      ? formValues.loraPath
                      : ""
                  }
                  onChange={(event) => onFieldChange("loraPath", event.target.value)}
                  disabled={disabled}
                >
                  <option value="">ベースモデルのみ</option>
                  {loraOptions.map((option) => (
                    <option key={option.id} value={option.path}>
                      {option.displayName}
                    </option>
                  ))}
                </Form.Select>
              </Form.Group>
            </Col>
          ) : null}
        </Row>
      </PanelFrame>
    );
  }

  if (mediaType === "audio") {
    return (
      <PanelFrame eyebrow="おすすめ設定" title="長さ・テンポ・雰囲気を先に決める">
        <Row className="g-3">
          <Col md={4}>
            <Form.Group>
              <Form.Label>長さ（秒）</Form.Label>
              <Form.Control
                type="number"
                inputMode="numeric"
                min="2"
                step="1"
                name="durationSeconds"
                value={formValues.durationSeconds}
                onChange={(event) => onFieldChange("durationSeconds", event.target.value)}
                disabled={disabled}
              />
            </Form.Group>
          </Col>
          <Col md={4}>
            <Form.Group>
              <Form.Label>BPM</Form.Label>
              <Form.Control
                type="number"
                inputMode="numeric"
                min="60"
                max="180"
                step="1"
                name="bpm"
                value={formValues.bpm}
                onChange={(event) => onFieldChange("bpm", event.target.value)}
                disabled={disabled}
              />
            </Form.Group>
          </Col>
          <Col md={4}>
            <Form.Group>
              <Form.Label>雰囲気</Form.Label>
              <Form.Select
                name="mood"
                value={formValues.mood}
                onChange={(event) => onFieldChange("mood", event.target.value)}
                disabled={disabled}
              >
                <option value="dreamy">やわらかい</option>
                <option value="bright">明るい</option>
                <option value="dark">暗め</option>
                <option value="energetic">元気</option>
                <option value="gentle">穏やか</option>
              </Form.Select>
            </Form.Group>
          </Col>
        </Row>
      </PanelFrame>
    );
  }

  return (
    <PanelFrame eyebrow="おすすめ設定" title="長さ・カメラ・見せ方を先に決める">
      <Row className="g-3">
        <Col md={4}>
          <Form.Group>
            <Form.Label>長さ（秒）</Form.Label>
            <Form.Control
              type="number"
              inputMode="numeric"
              min="2"
              step="1"
              name="durationSeconds"
              value={formValues.durationSeconds}
              onChange={(event) => onFieldChange("durationSeconds", event.target.value)}
              disabled={disabled}
            />
          </Form.Group>
        </Col>
        <Col md={4}>
          <Form.Group>
            <Form.Label>カメラ動作</Form.Label>
            <Form.Select
              name="cameraMotion"
              value={formValues.cameraMotion}
              onChange={(event) => onFieldChange("cameraMotion", event.target.value)}
              disabled={disabled}
            >
              <option value="push-in">寄る</option>
              <option value="orbit">回り込む</option>
              <option value="tilt-up">見上げる</option>
              <option value="lateral">横移動</option>
            </Form.Select>
          </Form.Group>
        </Col>
        <Col md={4}>
          <Form.Group>
            <Form.Label>画づくり</Form.Label>
            <Form.Select
              name="visualStyle"
              value={formValues.visualStyle}
              onChange={(event) => onFieldChange("visualStyle", event.target.value)}
              disabled={disabled}
            >
              <option value="storyboard">絵コンテ</option>
              <option value="editorial-board">エディトリアル</option>
              <option value="animatic">アニマティック</option>
              <option value="blocking-pass">ブロッキング</option>
            </Form.Select>
          </Form.Group>
        </Col>
      </Row>
    </PanelFrame>
  );
}

function renderAdvancedControls({
  mediaType,
  formValues,
  disabled,
  onFieldChange,
}: AdvancedControlsProps) {
  if (mediaType === "image") {
    return (
      <PanelFrame eyebrow="詳細設定" title="画角や再現性を細かく調整する">
        <Stack gap={3}>
          <Row className="g-3">
            <Col md={6}>
              <Form.Group>
                <Form.Label>Width</Form.Label>
                <Form.Control
                  type="number"
                  inputMode="numeric"
                  min="64"
                  step="64"
                  name="width"
                  value={formValues.width}
                  onChange={(event) => onFieldChange("width", event.target.value)}
                  disabled={disabled}
                />
              </Form.Group>
            </Col>
            <Col md={6}>
              <Form.Group>
                <Form.Label>Height</Form.Label>
                <Form.Control
                  type="number"
                  inputMode="numeric"
                  min="64"
                  step="64"
                  name="height"
                  value={formValues.height}
                  onChange={(event) => onFieldChange("height", event.target.value)}
                  disabled={disabled}
                />
              </Form.Group>
            </Col>
            <Col md={4}>
              <Form.Group>
                <Form.Label>Steps</Form.Label>
                <Form.Control
                  type="number"
                  inputMode="numeric"
                  min="1"
                  step="1"
                  name="steps"
                  value={formValues.steps}
                  onChange={(event) => onFieldChange("steps", event.target.value)}
                  disabled={disabled}
                />
              </Form.Group>
            </Col>
            <Col md={4}>
              <Form.Group>
                <Form.Label>ガイダンス</Form.Label>
                <Form.Control
                  type="number"
                  inputMode="decimal"
                  min="1"
                  step="0.1"
                  name="guidanceScale"
                  value={formValues.guidanceScale}
                  onChange={(event) => onFieldChange("guidanceScale", event.target.value)}
                  disabled={disabled}
                />
              </Form.Group>
            </Col>
            <Col md={4}>
              <Form.Group>
                <Form.Label>シード値</Form.Label>
                <Form.Control
                  type="number"
                  inputMode="numeric"
                  step="1"
                  name="seed"
                  placeholder="ランダム"
                  value={formValues.seed}
                  onChange={(event) => onFieldChange("seed", event.target.value)}
                  disabled={disabled}
                />
              </Form.Group>
            </Col>
            <Col md={6}>
              <Form.Group>
                <Form.Label>LoRA 強度</Form.Label>
                <Form.Control
                  type="number"
                  inputMode="decimal"
                  min="0"
                  step="0.05"
                  name="loraScale"
                  value={formValues.loraScale}
                  onChange={(event) => onFieldChange("loraScale", event.target.value)}
                  disabled={disabled}
                />
              </Form.Group>
            </Col>
            <Col md={6}>
              <Form.Group>
                <Form.Label>LoRA パス</Form.Label>
                <Form.Control
                  type="text"
                  name="loraPath"
                  placeholder="./models/loras/mai.safetensors"
                  value={formValues.loraPath}
                  onChange={(event) => onFieldChange("loraPath", event.target.value)}
                  disabled={disabled}
                />
              </Form.Group>
            </Col>
          </Row>
        </Stack>
      </PanelFrame>
    );
  }

  if (mediaType === "audio") {
    return (
      <PanelFrame eyebrow="詳細設定" title="再現性が必要なときだけ細かく指定する">
        <Row className="g-3">
          <Col md={6}>
            <Form.Group>
              <Form.Label>ガイダンス</Form.Label>
              <Form.Control
                type="number"
                inputMode="decimal"
                min="1"
                step="0.1"
                name="guidanceScale"
                value={formValues.guidanceScale}
                onChange={(event) => onFieldChange("guidanceScale", event.target.value)}
                disabled={disabled}
              />
            </Form.Group>
          </Col>
          <Col md={6}>
            <Form.Group>
              <Form.Label>シード値</Form.Label>
              <Form.Control
                type="number"
                inputMode="numeric"
                step="1"
                name="seed"
                placeholder="ランダム"
                value={formValues.seed}
                onChange={(event) => onFieldChange("seed", event.target.value)}
                disabled={disabled}
              />
            </Form.Group>
          </Col>
        </Row>
      </PanelFrame>
    );
  }

  return (
    <PanelFrame eyebrow="詳細設定" title="サイズやシードを指定して狙いを合わせる">
      <Row className="g-3">
        <Col md={4}>
          <Form.Group>
            <Form.Label>Width</Form.Label>
            <Form.Control
              type="number"
              inputMode="numeric"
              min="256"
              step="64"
              name="width"
              value={formValues.width}
              onChange={(event) => onFieldChange("width", event.target.value)}
              disabled={disabled}
            />
          </Form.Group>
        </Col>
        <Col md={4}>
          <Form.Group>
            <Form.Label>Height</Form.Label>
            <Form.Control
              type="number"
              inputMode="numeric"
              min="256"
              step="64"
              name="height"
              value={formValues.height}
              onChange={(event) => onFieldChange("height", event.target.value)}
              disabled={disabled}
            />
          </Form.Group>
        </Col>
        <Col md={4}>
          <Form.Group>
            <Form.Label>シード値</Form.Label>
            <Form.Control
              type="number"
              inputMode="numeric"
              step="1"
              name="seed"
              placeholder="ランダム"
              value={formValues.seed}
              onChange={(event) => onFieldChange("seed", event.target.value)}
              disabled={disabled}
            />
          </Form.Group>
        </Col>
      </Row>
    </PanelFrame>
  );
}
