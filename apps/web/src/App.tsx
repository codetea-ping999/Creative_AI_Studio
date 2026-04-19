import Alert from "react-bootstrap/Alert";
import Button from "react-bootstrap/Button";
import ButtonGroup from "react-bootstrap/ButtonGroup";
import Card from "react-bootstrap/Card";
import Col from "react-bootstrap/Col";
import Container from "react-bootstrap/Container";
import Row from "react-bootstrap/Row";
import Stack from "react-bootstrap/Stack";
import ToggleButton from "react-bootstrap/ToggleButton";
import { PromptForm } from "./components/PromptForm";
import type { MediaType } from "./components/promptFormTypes";
import { AssetDetailPanel } from "./components/AssetDetailPanel";
import { GallerySection } from "./components/GallerySection";
import { LatestJobPanel } from "./components/LatestJobPanel";
import { ProjectSidebar } from "./components/ProjectSidebar";
import { ProjectWorkspace } from "./components/ProjectWorkspace";
import { mediaTypeLabels } from "./studio";
import { useStudioController } from "./useStudioController";

function App() {
  const { state, derived, actions } = useStudioController();

  return (
    <Container fluid="xxl" className="studio-layout px-3 px-lg-4 py-4">
      {state.errorMessage ? (
        <Alert variant="danger" role="alert" aria-live="assertive" className="mb-4">
          {state.errorMessage}
        </Alert>
      ) : null}

      <Row className="g-4 align-items-start">
        <Col xs={{ order: 2 }} lg={{ span: 3, order: 1 }} className="studio-column">
          <Stack gap={3}>
            <Card className="studio-panel">
              <Card.Body>
                <Stack gap={3}>
                  <div className="sidebar-brand">
                    <p className="eyebrow">Creative AI Studio</p>
                    <h1 className="studio-brand__title">集中を邪魔しないローカル生成ワークスペース</h1>
                    <p className="sidebar-copy">
                      プロジェクトを選び、生成し、結果を見て、再利用までを同じ画面で進めます。
                    </p>
                  </div>

                  <div className="theme-switcher">
                    <p className="eyebrow mb-2">表示</p>
                    <ButtonGroup className="w-100" aria-label="表示テーマ">
                      <Button
                        type="button"
                        variant={state.themeMode === "light" ? "primary" : "outline-secondary"}
                        onClick={() => state.setThemeMode("light")}
                      >
                        ライト
                      </Button>
                      <Button
                        type="button"
                        variant={state.themeMode === "dark" ? "primary" : "outline-secondary"}
                        onClick={() => state.setThemeMode("dark")}
                      >
                        ダーク
                      </Button>
                    </ButtonGroup>
                  </div>
                </Stack>
              </Card.Body>
            </Card>

            <ProjectSidebar
              projects={state.projects}
              selectedProjectId={state.selectedProjectId}
              searchText={state.projectSearchText}
              statusFilter={state.projectStatusFilter}
              tagFilter={state.projectTagFilter}
              availableStatuses={derived.availableStatuses}
              availableTags={derived.availableTags}
              createProjectForm={state.createProjectForm}
              isProjectBusy={state.isProjectBusy}
              projectMessage={state.projectMessage}
              onSearchTextChange={state.setProjectSearchText}
              onStatusFilterChange={state.setProjectStatusFilter}
              onTagFilterChange={state.setProjectTagFilter}
              onSelectProject={actions.selectProject}
              onClearSelection={actions.clearProjectSelection}
              onCreateProjectFormChange={state.setCreateProjectForm}
              onCreateProject={() => {
                void actions.createProject();
              }}
            />
          </Stack>
        </Col>

        <Col xs={{ order: 1 }} lg={{ span: 6, order: 2 }} className="studio-column">
          <Card className="studio-panel studio-panel--main">
            <Card.Body>
              <Stack gap={4}>
                <div className="studio-main-header">
                  <div>
                    <p className="eyebrow">作成インターフェース</p>
                    <h2>
                      {derived.selectedProjectName
                        ? `${derived.selectedProjectName} で${mediaTypeLabels[state.mediaType]}を作る`
                        : "まずは作業文脈を選んで生成を始める"}
                    </h2>
                  </div>
                  <p className="section-footnote">
                    {state.selectedProjectData
                      ? "中央で生成し、結果を確認し、右で再利用へ進めます。"
                      : "左でプロジェクトを選ぶと、この中央面がその文脈に切り替わります。"}
                  </p>
                </div>

                <ProjectWorkspace
                  projectData={state.selectedProjectData}
                  isProjectBusy={state.isProjectBusy}
                  projectMessage={state.projectMessage}
                  onSaveProject={(values) => {
                    void actions.saveProject(values);
                  }}
                  onExportProject={() => {
                    void actions.exportProject();
                  }}
                  onRouteToComposer={actions.routeComposerToProject}
                  onDirtyChange={(dirty) => state.setIsProjectDirty(dirty)}
                />

                <div className="creator-toggle-grid">
                  <div className="creator-toggle-group">
                    <p className="eyebrow mb-2">メディア</p>
                    <ButtonGroup className="w-100 mode-toggle-group" aria-label="メディアモード">
                      {(["image", "audio", "video"] as MediaType[]).map((option) => (
                        <ToggleButton
                          key={option}
                          id={`media-type-${option}`}
                          type="radio"
                          name="media-type"
                          value={option}
                          variant={
                            state.mediaType === option ? "primary" : "outline-secondary"
                          }
                          checked={state.mediaType === option}
                          onChange={() => state.setMediaType(option)}
                          className="studio-toggle-button"
                        >
                          {mediaTypeLabels[option]}
                        </ToggleButton>
                      ))}
                    </ButtonGroup>
                  </div>

                  <div className="creator-toggle-group">
                    <p className="eyebrow mb-2">設定密度</p>
                    <ButtonGroup className="w-100 mode-toggle-group" aria-label="設定密度">
                      <ToggleButton
                        id={`control-mode-${state.mediaType}-quick`}
                        type="radio"
                        name={`control-mode-${state.mediaType}`}
                        value="quick"
                        variant={
                          derived.activeControlMode === "quick"
                            ? "primary"
                            : "outline-secondary"
                        }
                        checked={derived.activeControlMode === "quick"}
                        onChange={() =>
                          state.setControlModes((current) => ({
                            ...current,
                            [state.mediaType]: "quick",
                          }))
                        }
                        className="studio-toggle-button"
                      >
                        かんたん
                      </ToggleButton>
                      <ToggleButton
                        id={`control-mode-${state.mediaType}-advanced`}
                        type="radio"
                        name={`control-mode-${state.mediaType}`}
                        value="advanced"
                        variant={
                          derived.activeControlMode === "advanced"
                            ? "primary"
                            : "outline-secondary"
                        }
                        checked={derived.activeControlMode === "advanced"}
                        onChange={() =>
                          state.setControlModes((current) => ({
                            ...current,
                            [state.mediaType]: "advanced",
                          }))
                        }
                        className="studio-toggle-button"
                      >
                        詳細
                      </ToggleButton>
                    </ButtonGroup>
                  </div>
                </div>

                {state.selectedProjectData ? (
                  <PromptForm
                    key={`${state.mediaType}:${state.composerRevision}`}
                    formId="studio-prompt-form"
                    mediaType={state.mediaType}
                    controlMode={derived.activeControlMode}
                    onControlModeChange={(nextMode) =>
                      state.setControlModes((current) => ({
                        ...current,
                        [state.mediaType]: nextMode,
                      }))
                    }
                    modelOptions={derived.activeModels}
                    loraOptions={state.mediaType === "image" ? state.loraOptions : []}
                    initialValues={state.drafts[state.mediaType]}
                    submitLabel={state.isSubmitting ? "生成中..." : "生成を開始"}
                    disabled={state.isSubmitting}
                    canSubmit={!state.isSubmitting}
                    statusMessage={state.statusMessage}
                    onDraftChange={(nextDraft) =>
                      state.setDrafts((current) => ({
                        ...current,
                        [state.mediaType]: nextDraft,
                      }))
                    }
                    onSubmit={(values) => {
                      void actions.submitPrompt(values);
                    }}
                  />
                ) : (
                  <div className="empty-stage empty-stage--composer">
                    <div>
                      <h3>最初にプロジェクトを選んでください</h3>
                      <p>
                        左の一覧から選ぶか新しく作成すると、ここで{" "}
                        {mediaTypeLabels[state.mediaType]}の生成を始められます。
                      </p>
                    </div>
                  </div>
                )}

                <section className="review-surface">
                  <div className="review-surface__header">
                    <div>
                      <p className="eyebrow">確認する</p>
                      <h3>最新結果から履歴まで、中央導線のまま確認する</h3>
                    </div>
                    <p className="section-footnote">
                      最新結果を見たあと、そのまま履歴から次の素材を選べます。
                    </p>
                  </div>
                  <LatestJobPanel
                    latestJob={derived.reviewJob}
                    projectName={derived.selectedProjectName}
                    selectedAssetDetail={
                      state.selectedAssetDetail?.job_id === derived.reviewJob?.id
                        ? state.selectedAssetDetail
                        : null
                    }
                  />
                  <GallerySection
                    galleryItems={derived.prioritizedGalleryItems}
                    mediaType={state.mediaType}
                    selectedAssetId={state.selectedAssetId}
                    selectedProjectName={derived.selectedProjectName}
                    selectedProjectAssetCount={derived.selectedProjectGalleryCount}
                    onSelectAsset={(assetId) => {
                      void actions.selectAsset(assetId);
                    }}
                  />
                </section>
              </Stack>
            </Card.Body>
          </Card>
        </Col>

        <Col xs={{ order: 3 }} lg={{ span: 3, order: 3 }} className="studio-column">
          <AssetDetailPanel
            selectedAssetDetail={state.selectedAssetDetail}
            projects={state.projectCatalog}
            selectedAssetProjectId={state.selectedAssetProjectId}
            feedbackForm={state.feedbackForm}
            assetMessage={state.assetMessage}
            isAssetBusy={state.isAssetBusy}
            isFeedbackBusy={state.isFeedbackBusy}
            onAssetProjectIdChange={state.setSelectedAssetProjectId}
            onFeedbackFormChange={state.setFeedbackForm}
            onReuseAsset={() => {
              void actions.reuseSelectedAsset();
            }}
            onLoadIntoComposer={actions.loadSelectedAssetIntoComposer}
            onExportAsset={() => {
              void actions.exportSelectedAsset();
            }}
            onUpdateAssetProject={() => {
              void actions.updateSelectedAssetProject();
            }}
            onSubmitFeedback={() => {
              void actions.submitFeedback();
            }}
          />
        </Col>
      </Row>
    </Container>
  );
}

export default App;
