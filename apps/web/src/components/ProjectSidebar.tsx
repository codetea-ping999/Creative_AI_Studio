import Accordion from "react-bootstrap/Accordion";
import Badge from "react-bootstrap/Badge";
import Button from "react-bootstrap/Button";
import Card from "react-bootstrap/Card";
import Form from "react-bootstrap/Form";
import ListGroup from "react-bootstrap/ListGroup";
import Stack from "react-bootstrap/Stack";
import type { ProjectFormValues, ProjectResponse } from "../studio";
import { createOutputUrl, isAudioAsset, isVideoAsset } from "../studio";
import { ProjectForm } from "./ProjectForm";

type ProjectSidebarProps = {
  projects: ProjectResponse[];
  selectedProjectId: string;
  searchText: string;
  statusFilter: string;
  tagFilter: string;
  availableStatuses: string[];
  availableTags: string[];
  createProjectForm: ProjectFormValues;
  isProjectBusy: boolean;
  projectMessage: string | null;
  onSearchTextChange: (nextValue: string) => void;
  onStatusFilterChange: (nextValue: string) => void;
  onTagFilterChange: (nextValue: string) => void;
  onSelectProject: (projectId: string) => void;
  onClearSelection: () => void;
  onCreateProjectFormChange: (nextValue: ProjectFormValues) => void;
  onCreateProject: () => void;
};

export function ProjectSidebar({
  projects,
  selectedProjectId,
  searchText,
  statusFilter,
  tagFilter,
  availableStatuses,
  availableTags,
  createProjectForm,
  isProjectBusy,
  projectMessage,
  onSearchTextChange,
  onStatusFilterChange,
  onTagFilterChange,
  onSelectProject,
  onClearSelection,
  onCreateProjectFormChange,
  onCreateProject,
}: ProjectSidebarProps) {
  return (
    <Stack gap={3}>
      <Card className="studio-panel">
        <Card.Body>
          <Stack gap={3}>
            <div className="studio-panel__header">
              <div>
                <p className="eyebrow">プロジェクト</p>
                <h2 className="studio-panel__title">作業文脈を選ぶ</h2>
              </div>
              <Button
                type="button"
                variant="outline-secondary"
                size="sm"
                onClick={onClearSelection}
                disabled={!selectedProjectId}
              >
                選択を解除
              </Button>
            </div>

            <Form className="project-filter-grid">
              <Form.Group className="filter-search">
                <Form.Label>検索</Form.Label>
                <Form.Control
                  type="search"
                  value={searchText}
                  onChange={(event) => onSearchTextChange(event.target.value)}
                  placeholder="プロジェクト名、タグ、メタデータで検索"
                />
              </Form.Group>

              <Form.Group>
                <Form.Label>状態</Form.Label>
                <Form.Select
                  value={statusFilter}
                  onChange={(event) => onStatusFilterChange(event.target.value)}
                >
                  <option value="">すべて</option>
                  {availableStatuses.map((status) => (
                    <option key={status} value={status}>
                      {status}
                    </option>
                  ))}
                </Form.Select>
              </Form.Group>

              <Form.Group>
                <Form.Label>タグ</Form.Label>
                <Form.Select
                  value={tagFilter}
                  onChange={(event) => onTagFilterChange(event.target.value)}
                >
                  <option value="">すべて</option>
                  {availableTags.map((tag) => (
                    <option key={tag} value={tag}>
                      {tag}
                    </option>
                  ))}
                </Form.Select>
              </Form.Group>
            </Form>

            {projectMessage ? <p className="section-footnote">{projectMessage}</p> : null}

            {projects.length > 0 ? (
              <ListGroup className="project-list">
                {projects.map((project) => (
                  <ListGroup.Item
                    key={project.id}
                    as="button"
                    type="button"
                    action
                    active={project.id === selectedProjectId}
                    className="project-list-item"
                    onClick={() => onSelectProject(project.id)}
                  >
                    <ProjectCardCover coverPath={project.cover_asset_path} />
                    <div className="project-card__body">
                      <div className="project-card__header">
                        <div>
                          <strong className="project-card__title">{project.name}</strong>
                          <p
                            className="project-card__description"
                            title={project.description || undefined}
                          >
                            {project.description ||
                              "説明はまだありません。必要なら下の編集欄から追加できます。"}
                          </p>
                        </div>
                        <Badge bg="secondary" pill>
                          {project.status}
                        </Badge>
                      </div>

                      <div className="asset-chip-row">
                        <span className="info-chip">素材 {project.asset_count} 件</span>
                        <span className="info-chip">生成 {project.job_count} 件</span>
                      </div>

                      {project.tags.length > 0 ? (
                        <div className="tag-list">
                          {project.tags.map((tag) => (
                            <span key={`${project.id}:${tag}`} className="tag-pill">
                              {tag}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  </ListGroup.Item>
                ))}
              </ListGroup>
            ) : (
              <div className="history-empty">
                条件に合うプロジェクトがありません。検索条件を緩めるか、下から新規作成してください。
              </div>
            )}
          </Stack>
        </Card.Body>
      </Card>

      <Accordion className="studio-accordion">
        <Accordion.Item eventKey="create">
          <Accordion.Header>新しい作業文脈を追加する</Accordion.Header>
          <Accordion.Body>
            <ProjectForm
              eyebrow="新規プロジェクト"
              title="一覧に追加する"
              description="作成すると、この左の一覧からすぐ選んで中央の生成面に移れます。"
              submitLabel={isProjectBusy ? "作成中..." : "プロジェクトを作成"}
              value={createProjectForm}
              disabled={isProjectBusy}
              onChange={onCreateProjectFormChange}
              onSubmit={onCreateProject}
            />
          </Accordion.Body>
        </Accordion.Item>
      </Accordion>
    </Stack>
  );
}

type ProjectCardCoverProps = {
  coverPath: string | null;
};

function ProjectCardCover({ coverPath }: ProjectCardCoverProps) {
  const src = createOutputUrl(coverPath);

  if (!src) {
    return (
      <div className="project-card__cover is-empty">
        <span>カバー未設定</span>
      </div>
    );
  }

  if (isAudioAsset(coverPath)) {
    return (
      <div className="project-card__cover is-audio">
        <span>音声</span>
      </div>
    );
  }

  if (isVideoAsset(coverPath)) {
    return (
      <div className="project-card__cover">
        <video muted playsInline preload="metadata" src={src} />
      </div>
    );
  }

  return (
    <div className="project-card__cover">
      <img src={src} alt="" loading="lazy" />
    </div>
  );
}
