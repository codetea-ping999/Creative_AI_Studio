import { useEffect, useRef, useState } from "react";
import Accordion from "react-bootstrap/Accordion";
import Button from "react-bootstrap/Button";
import Row from "react-bootstrap/Row";
import Col from "react-bootstrap/Col";
import Stack from "react-bootstrap/Stack";
import type { ProjectFormValues, ProjectJobsResponse } from "../studio";
import {
  areProjectFormValuesEqual,
  formatDate,
  formatScore,
  mediaTypeLabels,
  projectToFormValues,
  serializeProjectFormValues,
} from "../studio";
import { ProjectForm } from "./ProjectForm";

type ProjectWorkspaceProps = {
  projectData: ProjectJobsResponse | null;
  isProjectBusy: boolean;
  projectMessage: string | null;
  onSaveProject: (values: ProjectFormValues) => void;
  onExportProject: () => void;
  onRouteToComposer: () => void;
  onDirtyChange?: (isDirty: boolean) => void;
};

export function ProjectWorkspace({
  projectData,
  isProjectBusy,
  projectMessage,
  onSaveProject,
  onExportProject,
  onRouteToComposer,
  onDirtyChange,
}: ProjectWorkspaceProps) {
  const initialForm = projectToFormValues(projectData?.project);
  const [editForm, setEditForm] = useState<ProjectFormValues>(initialForm);
  const [baselineForm, setBaselineForm] = useState<ProjectFormValues>(initialForm);
  const previousProjectIdRef = useRef<string | null>(projectData?.project.id ?? null);
  const isDirty = !areProjectFormValuesEqual(editForm, baselineForm);
  const baselineSignature = serializeProjectFormValues(baselineForm);

  useEffect(() => {
    onDirtyChange?.(isDirty);
  }, [isDirty, onDirtyChange]);

  useEffect(() => {
    const nextProjectId = projectData?.project.id ?? null;
    const nextForm = projectToFormValues(projectData?.project);
    const nextSignature = serializeProjectFormValues(nextForm);
    const projectChanged = previousProjectIdRef.current !== nextProjectId;

    if (projectChanged) {
      previousProjectIdRef.current = nextProjectId;
      setBaselineForm(nextForm);
      setEditForm(nextForm);
      return;
    }

    if (nextSignature === baselineSignature) {
      return;
    }

    setBaselineForm(nextForm);
    setEditForm((current) =>
      serializeProjectFormValues(current) === nextSignature ? nextForm : current,
    );
  }, [baselineSignature, projectData]);

  if (!projectData) {
    return (
      <div className="context-summary context-summary--empty">
        <p className="eyebrow">現在の作業文脈</p>
        <h3>左の一覧からプロジェクトを選んでください</h3>
        <p className="section-footnote">
          プロジェクトを選ぶと、この中央面で生成、確認、再利用まで続けて進められます。
        </p>
      </div>
    );
  }

  const metadataEntries = Object.entries(projectData.project.metadata);
  const visibleMetadataEntries = metadataEntries.slice(0, 6);

  return (
    <div className="context-summary">
      <Stack gap={3}>
        <div className="context-summary__header">
          <div>
            <p className="eyebrow">現在の作業文脈</p>
            <h3>{projectData.project.name}</h3>
          </div>
          {projectMessage ? <p className="section-footnote">{projectMessage}</p> : null}
        </div>

        <p className="context-summary__lead">
          {projectData.project.description ||
            "このプロジェクトで何を作るかを短く残しておくと、次の生成判断がぶれにくくなります。"}
        </p>

        <div className="asset-chip-row">
          <span className="info-chip">{projectData.project.status}</span>
          <span className="info-chip">素材 {projectData.asset_count} 件</span>
          <span className="info-chip">生成 {projectData.job_count} 件</span>
          <span className="info-chip">品質 {formatScore(projectData.average_quality_score)}</span>
        </div>

        {projectData.project.tags.length > 0 ? (
          <div className="tag-list">
            {projectData.project.tags.map((tag) => (
              <span key={`${projectData.project.id}:${tag}`} className="tag-pill">
                {tag}
              </span>
            ))}
          </div>
        ) : null}

        <Row className="g-3 metadata-grid">
          <Col sm={6}>
            <div className="metadata-item">
              <span>更新日時</span>
              <strong>{formatDate(projectData.project.updated_at)}</strong>
            </div>
          </Col>
          <Col sm={6}>
            <div className="metadata-item">
              <span>固定素材</span>
              <strong>{projectData.project.pinned_asset_ids.length}</strong>
            </div>
          </Col>
          <Col sm={6}>
            <div className="metadata-item">
              <span>創造性</span>
              <strong>{formatScore(projectData.average_creative_alignment_score)}</strong>
            </div>
          </Col>
          <Col sm={6}>
            <div className="metadata-item">
              <span>進行中の内容</span>
              <strong>
                {Object.entries(projectData.media_breakdown).length > 0
                  ? Object.entries(projectData.media_breakdown)
                      .map(
                        ([itemMediaType, count]) =>
                          `${mediaTypeLabels[itemMediaType as keyof typeof mediaTypeLabels]} ${count}件`,
                      )
                      .join(" / ")
                  : "まだ生成履歴はありません"}
              </strong>
            </div>
          </Col>
        </Row>

        <div className="d-flex justify-content-start">
          <Button type="button" onClick={onRouteToComposer} disabled={isProjectBusy}>
            このプロジェクトで生成する
          </Button>
        </div>

        <Accordion className="studio-accordion">
          <Accordion.Item eventKey="details">
            <Accordion.Header>詳細と編集を開く</Accordion.Header>
            <Accordion.Body>
              <Stack gap={3}>
                <div className="d-flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline-secondary"
                    onClick={onExportProject}
                    disabled={isProjectBusy}
                  >
                    {isProjectBusy ? "書き出し中..." : "プロジェクトを書き出す"}
                  </Button>
                </div>

                {visibleMetadataEntries.length > 0 ? (
                  <div className="editor-block">
                    <div className="studio-panel__header">
                      <div>
                        <p className="eyebrow">補足情報</p>
                        <h3 className="form-panel__title">保存されているメタデータ</h3>
                      </div>
                    </div>
                    <Row className="g-3 metadata-grid">
                      {visibleMetadataEntries.map(([key, value]) => (
                        <Col sm={6} key={key}>
                          <div className="metadata-item">
                            <span>{key}</span>
                            <strong>{value == null ? "" : String(value)}</strong>
                          </div>
                        </Col>
                      ))}
                    </Row>
                  </div>
                ) : null}

                <ProjectForm
                  eyebrow="プロジェクト編集"
                  title="名前やタグを整える"
                  description="作業文脈を見失わないよう、必要な情報だけをここで更新します。"
                  submitLabel={isProjectBusy ? "保存中..." : "変更を保存"}
                  value={editForm}
                  disabled={isProjectBusy}
                  onChange={setEditForm}
                  onSubmit={() => onSaveProject(editForm)}
                />
              </Stack>
            </Accordion.Body>
          </Accordion.Item>
        </Accordion>
      </Stack>
    </div>
  );
}
