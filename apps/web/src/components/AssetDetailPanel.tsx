import Accordion from "react-bootstrap/Accordion";
import Button from "react-bootstrap/Button";
import Card from "react-bootstrap/Card";
import Col from "react-bootstrap/Col";
import Form from "react-bootstrap/Form";
import Row from "react-bootstrap/Row";
import Stack from "react-bootstrap/Stack";
import type { GalleryAssetDetailResponse, ProjectResponse } from "../studio";
import {
  formatDate,
  formatScore,
  formatSemanticStatus,
  type FeedbackFormValues,
} from "../studio";
import { StagePreview } from "./StagePreview";

const ISSUE_TAG_OPTIONS = [
  { value: "prompt_mismatch", label: "意図ずれ" },
  { value: "quality_artifact", label: "破綻" },
  { value: "composition", label: "構図" },
  { value: "audio_noise", label: "ノイズ" },
  { value: "motion", label: "動き" },
];

type AssetDetailPanelProps = {
  selectedAssetDetail: GalleryAssetDetailResponse | null;
  projects: ProjectResponse[];
  selectedAssetProjectId: string;
  feedbackForm: FeedbackFormValues;
  assetMessage: string | null;
  isAssetBusy: boolean;
  isFeedbackBusy: boolean;
  onAssetProjectIdChange: (projectId: string) => void;
  onFeedbackFormChange: (nextValue: FeedbackFormValues) => void;
  onReuseAsset: () => void;
  onLoadIntoComposer: () => void;
  onExportAsset: () => void;
  onUpdateAssetProject: () => void;
  onSubmitFeedback: () => void;
};

export function AssetDetailPanel({
  selectedAssetDetail,
  projects,
  selectedAssetProjectId,
  feedbackForm,
  assetMessage,
  isAssetBusy,
  isFeedbackBusy,
  onAssetProjectIdChange,
  onFeedbackFormChange,
  onReuseAsset,
  onLoadIntoComposer,
  onExportAsset,
  onUpdateAssetProject,
  onSubmitFeedback,
}: AssetDetailPanelProps) {
  const feedbackSummary = selectedAssetDetail?.feedback_summary ?? {};
  const humanQualityScore =
    typeof feedbackSummary.human_quality_score === "number"
      ? feedbackSummary.human_quality_score
      : null;
  const humanSemanticScore =
    typeof feedbackSummary.human_semantic_alignment_score === "number"
      ? feedbackSummary.human_semantic_alignment_score
      : null;
  const humanCreativeScore =
    typeof feedbackSummary.human_creative_alignment_score === "number"
      ? feedbackSummary.human_creative_alignment_score
      : null;

  return (
    <Card className="studio-panel studio-panel--inspector">
      <Card.Body>
        <Stack gap={3}>
          <div className="studio-panel__header">
            <div>
              <p className="eyebrow">右インスペクタ</p>
              <h2 className="studio-panel__title">
                {selectedAssetDetail ? "選択中の素材を活かす" : "素材を選ぶと次の操作が出ます"}
              </h2>
            </div>
            {assetMessage ? <p className="section-footnote">{assetMessage}</p> : null}
          </div>

          {selectedAssetDetail ? (
            <>
              <StagePreview
                mediaType={selectedAssetDetail.media_type}
                outputPath={selectedAssetDetail.preview_path}
                title={selectedAssetDetail.prompt}
                subtitle={selectedAssetDetail.project_name || "未割り当て"}
              />

              <div className="asset-actions asset-actions--primary">
                <Button type="button" onClick={onReuseAsset} disabled={isAssetBusy}>
                  再利用して生成
                </Button>
                <Button
                  type="button"
                  variant="outline-secondary"
                  onClick={onLoadIntoComposer}
                  disabled={isAssetBusy}
                >
                  Composer に読み込む
                </Button>
                <Button
                  type="button"
                  variant="outline-secondary"
                  onClick={onExportAsset}
                  disabled={isAssetBusy}
                >
                  素材を書き出す
                </Button>
              </div>

              <div className="asset-assignment">
                <Stack gap={3}>
                  <div>
                    <p className="eyebrow">プロジェクト割り当て</p>
                    <h3 className="form-panel__title">この素材の所属先を変更する</h3>
                  </div>

                  <Form.Group>
                    <Form.Label>割り当て先</Form.Label>
                    <Form.Select
                      value={selectedAssetProjectId}
                      onChange={(event) => onAssetProjectIdChange(event.target.value)}
                      disabled={isAssetBusy}
                    >
                      <option value="">未割り当て</option>
                      {projects.map((project) => (
                        <option key={project.id} value={project.id}>
                          {project.name}
                        </option>
                      ))}
                    </Form.Select>
                  </Form.Group>

                  <div className="d-flex justify-content-end">
                    <Button
                      type="button"
                      variant="outline-secondary"
                      onClick={onUpdateAssetProject}
                      disabled={isAssetBusy}
                    >
                      割り当てを更新
                    </Button>
                  </div>
                </Stack>
              </div>

              <div className="editor-block">
                <div className="studio-panel__header">
                  <div>
                    <p className="eyebrow">評価状態</p>
                    <h3 className="form-panel__title">自動評価と人手補正を確認する</h3>
                  </div>
                </div>

                <div className="asset-chip-row">
                  <span className="info-chip">
                    semantic {formatSemanticStatus(selectedAssetDetail.semantic_status)}
                  </span>
                  {selectedAssetDetail.semantic_backend ? (
                    <span className="info-chip">{selectedAssetDetail.semantic_backend}</span>
                  ) : null}
                  <span className="info-chip">FB {selectedAssetDetail.feedback_count}</span>
                </div>

                {selectedAssetDetail.semantic_reason ? (
                  <p className="section-footnote">{selectedAssetDetail.semantic_reason}</p>
                ) : null}

                <Row className="g-3 metadata-grid">
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>自動品質</span>
                      <strong>{formatScore(selectedAssetDetail.quality_score)}</strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>補正後品質</span>
                      <strong>{formatScore(selectedAssetDetail.quality_score_calibrated)}</strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>人手品質</span>
                      <strong>{formatScore(humanQualityScore)}</strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>自動意味一致</span>
                      <strong>{formatScore(selectedAssetDetail.semantic_alignment_score)}</strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>補正後意味一致</span>
                      <strong>
                        {formatScore(selectedAssetDetail.semantic_alignment_score_calibrated)}
                      </strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>人手意味一致</span>
                      <strong>{formatScore(humanSemanticScore)}</strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>自動創造性</span>
                      <strong>{formatScore(selectedAssetDetail.creative_alignment_score)}</strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>補正後創造性</span>
                      <strong>
                        {formatScore(selectedAssetDetail.creative_alignment_score_calibrated)}
                      </strong>
                    </div>
                  </Col>
                  <Col sm={4}>
                    <div className="metadata-item">
                      <span>人手創造性</span>
                      <strong>{formatScore(humanCreativeScore)}</strong>
                    </div>
                  </Col>
                </Row>
              </div>

              <div className="editor-block">
                <div className="studio-panel__header">
                  <div>
                    <p className="eyebrow">フィードバック</p>
                    <h3 className="form-panel__title">この素材の評価を追加する</h3>
                  </div>
                </div>

                <Stack gap={3}>
                  <Row className="g-3">
                    <Col sm={4}>
                      <Form.Group>
                        <Form.Label>品質</Form.Label>
                        <Form.Select
                          value={feedbackForm.qualityRating}
                          onChange={(event) =>
                            onFeedbackFormChange({
                              ...feedbackForm,
                              qualityRating: Number(event.target.value),
                            })
                          }
                          disabled={isFeedbackBusy}
                        >
                          {[1, 2, 3, 4, 5].map((value) => (
                            <option key={`quality-${value}`} value={value}>
                              {value}
                            </option>
                          ))}
                        </Form.Select>
                      </Form.Group>
                    </Col>
                    <Col sm={4}>
                      <Form.Group>
                        <Form.Label>意味一致</Form.Label>
                        <Form.Select
                          value={feedbackForm.semanticRating}
                          onChange={(event) =>
                            onFeedbackFormChange({
                              ...feedbackForm,
                              semanticRating: Number(event.target.value),
                            })
                          }
                          disabled={isFeedbackBusy}
                        >
                          {[1, 2, 3, 4, 5].map((value) => (
                            <option key={`semantic-${value}`} value={value}>
                              {value}
                            </option>
                          ))}
                        </Form.Select>
                      </Form.Group>
                    </Col>
                    <Col sm={4}>
                      <Form.Group>
                        <Form.Label>創造性</Form.Label>
                        <Form.Select
                          value={feedbackForm.creativeRating}
                          onChange={(event) =>
                            onFeedbackFormChange({
                              ...feedbackForm,
                              creativeRating: Number(event.target.value),
                            })
                          }
                          disabled={isFeedbackBusy}
                        >
                          {[1, 2, 3, 4, 5].map((value) => (
                            <option key={`creative-${value}`} value={value}>
                              {value}
                            </option>
                          ))}
                        </Form.Select>
                      </Form.Group>
                    </Col>
                  </Row>

                  <Row className="g-3">
                    <Col sm={6}>
                      <Form.Check
                        id="feedback-reuse-intent"
                        type="switch"
                        label="再利用したい"
                        checked={feedbackForm.reuseIntent}
                        disabled={isFeedbackBusy}
                        onChange={(event) =>
                          onFeedbackFormChange({
                            ...feedbackForm,
                            reuseIntent: event.target.checked,
                          })
                        }
                      />
                    </Col>
                    <Col sm={6}>
                      <Form.Check
                        id="feedback-export-ready"
                        type="switch"
                        label="書き出し候補"
                        checked={feedbackForm.exportReady}
                        disabled={isFeedbackBusy}
                        onChange={(event) =>
                          onFeedbackFormChange({
                            ...feedbackForm,
                            exportReady: event.target.checked,
                          })
                        }
                      />
                    </Col>
                  </Row>

                  <Form.Group>
                    <Form.Label>気になった点</Form.Label>
                    <div className="tag-list">
                      {ISSUE_TAG_OPTIONS.map((option) => {
                        const checked = feedbackForm.issueTags.includes(option.value);
                        return (
                          <Form.Check
                            key={option.value}
                            id={`issue-tag-${option.value}`}
                            type="checkbox"
                            label={option.label}
                            checked={checked}
                            disabled={isFeedbackBusy}
                            onChange={(event) =>
                              onFeedbackFormChange({
                                ...feedbackForm,
                                issueTags: event.target.checked
                                  ? [...feedbackForm.issueTags, option.value]
                                  : feedbackForm.issueTags.filter((item) => item !== option.value),
                              })
                            }
                          />
                        );
                      })}
                    </div>
                  </Form.Group>

                  <Form.Group>
                    <Form.Label>コメント</Form.Label>
                    <Form.Control
                      as="textarea"
                      rows={3}
                      value={feedbackForm.comments}
                      disabled={isFeedbackBusy}
                      onChange={(event) =>
                        onFeedbackFormChange({
                          ...feedbackForm,
                          comments: event.target.value,
                        })
                      }
                    />
                  </Form.Group>

                  <div className="d-flex justify-content-end">
                    <Button
                      type="button"
                      onClick={onSubmitFeedback}
                      disabled={isFeedbackBusy}
                    >
                      {isFeedbackBusy ? "保存中..." : "フィードバックを保存"}
                    </Button>
                  </div>
                </Stack>
              </div>

              <Row className="g-3 metadata-grid">
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>素材ID</span>
                    <strong>{selectedAssetDetail.asset_id}</strong>
                  </div>
                </Col>
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>モデル</span>
                    <strong>{selectedAssetDetail.model_id}</strong>
                  </div>
                </Col>
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>作成日時</span>
                    <strong>{formatDate(selectedAssetDetail.created_at)}</strong>
                  </div>
                </Col>
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>更新日時</span>
                    <strong>{formatDate(selectedAssetDetail.updated_at)}</strong>
                  </div>
                </Col>
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>品質</span>
                    <strong>{formatScore(selectedAssetDetail.quality_score)}</strong>
                  </div>
                </Col>
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>意味一致</span>
                    <strong>{formatScore(selectedAssetDetail.semantic_alignment_score)}</strong>
                  </div>
                </Col>
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>創造性</span>
                    <strong>{formatScore(selectedAssetDetail.creative_alignment_score)}</strong>
                  </div>
                </Col>
                <Col sm={6}>
                  <div className="metadata-item">
                    <span>フィードバック</span>
                    <strong>{selectedAssetDetail.feedback_count}</strong>
                  </div>
                </Col>
              </Row>

              <Accordion className="studio-accordion">
                <Accordion.Item eventKey="history">
                  <Accordion.Header>利用履歴</Accordion.Header>
                  <Accordion.Body>
                    <Stack gap={3}>
                      <div className="asset-chip-row">
                        <span className="info-chip">再利用 {selectedAssetDetail.reuse_count}</span>
                        <span className="info-chip">書き出し {selectedAssetDetail.export_count}</span>
                        <span className="info-chip">FB {selectedAssetDetail.feedback_count}</span>
                      </div>

                      <div className="asset-list">
                        <div className="asset-path">
                          <span>元素材</span>
                          <code>{selectedAssetDetail.parent_asset_id ?? "なし"}</code>
                        </div>
                        <div className="asset-path">
                          <span>派生履歴</span>
                          <code>
                            {selectedAssetDetail.lineage.length > 0
                              ? selectedAssetDetail.lineage.join(", ")
                              : "なし"}
                          </code>
                        </div>
                        <div className="asset-path">
                          <span>書き出し先</span>
                          <code>
                            {selectedAssetDetail.export_paths.length > 0
                              ? selectedAssetDetail.export_paths.join(", ")
                              : "なし"}
                          </code>
                        </div>
                      </div>
                    </Stack>
                  </Accordion.Body>
                </Accordion.Item>

                <Accordion.Item eventKey="settings">
                  <Accordion.Header>生成時の詳細設定</Accordion.Header>
                  <Accordion.Body>
                    <pre className="json-block">
                      {JSON.stringify(selectedAssetDetail.request_snapshot, null, 2)}
                    </pre>
                  </Accordion.Body>
                </Accordion.Item>
              </Accordion>
            </>
          ) : (
            <div className="empty-stage empty-stage--inspector">
              <div>
                <h3>まだ素材は選ばれていません</h3>
                <p>
                  中央の確認エリアで素材を選ぶと、ここから再利用、読み込み、書き出しをすぐ行えます。
                </p>
              </div>
            </div>
          )}
        </Stack>
      </Card.Body>
    </Card>
  );
}
