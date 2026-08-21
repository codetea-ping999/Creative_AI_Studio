import { requestJson } from "../studioClient";

export type StorySummary = {
  id: string;
  title: string;
  project_id: string | null;
  logline: string;
  format: string;
  structure: string;
  language: string;
  beat_count: number;
  scene_count: number;
  chapter_count: number;
  total_duration_seconds: number;
  created_at: string;
  updated_at: string;
};

export type StoryBeat = {
  id: string;
  act: string;
  purpose: string;
  summary: string;
  order: number;
};

export type StoryScene = {
  id: string;
  order: number;
  beat_id?: string | null;
  heading: string;
  summary: string;
  narration: string;
  dialogue?: Array<{ speaker: string; text: string; direction?: string | null }>;
  image_prompt: string;
  image_negative: string;
  bgm_mood: string;
  duration_seconds: number;
  camera: string;
  bible_refs?: string[];
  asset_ids: Record<string, string>;
  job_ids?: string[];
};

export type StoryChapter = {
  id: string;
  order?: number;
  title: string;
  prose_markdown?: string;
  word_count: number;
};

export type StoryDocument = {
  id: string;
  title: string;
  project_id: string | null;
  logline: string;
  premise: string;
  genre?: string;
  tone?: string;
  audience?: string;
  language: string;
  format: string;
  structure: string;
  characters?: string[];
  beats: StoryBeat[];
  scenes: StoryScene[];
  chapters: StoryChapter[];
  metadata: Record<string, unknown>;
  source_job_ids: string[];
};

export type MissingAsset = { scene_id: string; role: string };

export type StoryDetail = {
  story: StoryDocument;
  missing_assets: MissingAsset[];
};

export type StoryListResponse = {
  items: StorySummary[];
  formats: string[];
};

/** The writing stages a story can be expanded through, in production order. */
export const storyStages = [
  { task: "logline", label: "Logline", hint: "前提から候補を出す" },
  { task: "beat_sheet", label: "Beats", hint: "構成に分解する" },
  { task: "scene_list", label: "Scenes", hint: "カットに落とす" },
  { task: "script", label: "Script", hint: "台詞を書く" },
  { task: "prose", label: "Prose", hint: "本文を書く" },
] as const;

export type StoryStageTask = (typeof storyStages)[number]["task"];

/**
 * Stages that write into one scene and must be told which one.
 *
 * The writer model is given a scene brief, not the story's scene ids, so the
 * target travels on the request. Without it the API refuses the job rather than
 * generating dialogue that has nowhere to land.
 */
export const sceneScopedStages: ReadonlySet<string> = new Set(["script"]);

/**
 * Keep a scene selection usable across regenerations.
 *
 * Rewriting the scene list can drop the id that was selected; falling back to
 * the first scene keeps the stage reachable instead of silently disabling it.
 */
export function resolveSceneTarget(
  story: StoryDocument | null,
  selected: string,
): string {
  const scenes = [...(story?.scenes ?? [])].sort(
    (left, right) => left.order - right.order,
  );
  if (scenes.length === 0) {
    return "";
  }
  return scenes.some((scene) => scene.id === selected) ? selected : scenes[0].id;
}

export function listStories(options: {
  projectId?: string;
  query?: string;
  limit?: number;
} = {}): Promise<StoryListResponse> {
  const search = new URLSearchParams();
  if (options.projectId) {
    search.set("project_id", options.projectId);
  }
  if (options.query?.trim()) {
    search.set("query", options.query.trim());
  }
  if (options.limit) {
    search.set("limit", String(options.limit));
  }
  const query = search.toString();
  return requestJson<StoryListResponse>(`/stories${query ? `?${query}` : ""}`);
}

export function getStory(storyId: string): Promise<StoryDetail> {
  return requestJson<StoryDetail>(`/stories/${storyId}`);
}

export function createStory(payload: {
  title: string;
  premise: string;
  project_id?: string | null;
  format?: string;
  structure?: string;
  genre?: string;
  tone?: string;
  audience?: string;
  characters?: string[];
}): Promise<StorySummary> {
  return requestJson<StorySummary>("/stories", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: payload.title,
      premise: payload.premise,
      project_id: payload.project_id || null,
      ...(payload.format ? { format: payload.format } : {}),
      ...(payload.structure ? { structure: payload.structure } : {}),
      ...(payload.genre ? { genre: payload.genre } : {}),
      ...(payload.tone ? { tone: payload.tone } : {}),
      ...(payload.audience ? { audience: payload.audience } : {}),
      ...(payload.characters?.length ? { characters: payload.characters } : {}),
    }),
  });
}

export function updateStory(
  storyId: string,
  payload: Partial<{
    title: string;
    premise: string;
    logline: string;
    genre: string;
    tone: string;
    audience: string;
    format: string;
    structure: string;
    characters: string[];
  }>,
): Promise<StorySummary> {
  return requestJson<StorySummary>(`/stories/${storyId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function expandStory(
  storyId: string,
  payload: { task: string; model_id: string; params?: Record<string, unknown> },
): Promise<{ job_id: string; status: string }> {
  return requestJson<{ job_id: string; status: string }>(
    `/stories/${storyId}/expand`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task: payload.task,
        model_id: payload.model_id,
        params: payload.params ?? {},
      }),
    },
  );
}

export function applyStoryResult(
  storyId: string,
  jobId: string,
): Promise<StoryDetail> {
  return requestJson<StoryDetail>(`/stories/${storyId}/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId }),
  });
}

/**
 * Which stages are reachable right now.
 *
 * Writing is sequential: beats need a logline, scenes need beats, and dialogue or
 * prose need scenes. Disabling the unreachable stages is what stops a user from
 * queueing a job whose brief would be empty.
 */
export function availableStages(story: StoryDocument | null): Set<string> {
  const available = new Set<string>();
  if (!story) {
    return available;
  }
  if (story.premise.trim() || story.logline.trim() || story.title.trim()) {
    available.add("logline");
  }
  if (story.logline.trim()) {
    available.add("beat_sheet");
  }
  if (story.beats.length > 0) {
    available.add("scene_list");
  }
  if (story.scenes.length > 0) {
    available.add("script");
    available.add("prose");
  }
  return available;
}

export function loglineCandidates(story: StoryDocument | null): string[] {
  const raw = story?.metadata?.logline_candidates;
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .map((entry) =>
      entry && typeof entry === "object" && typeof (entry as { text?: unknown }).text === "string"
        ? (entry as { text: string }).text
        : "",
    )
    .filter(Boolean);
}

export type SceneRole = "visual" | "narration" | "music";

export const sceneRoleLabels: Record<SceneRole, string> = {
  visual: "画像",
  narration: "ナレーション",
  music: "BGM",
};

/**
 * Generate one role of one scene.
 *
 * The result binds itself back to the scene on the server, so the caller only
 * has to refresh the story once the job finishes.
 */
export function generateSceneMedia(
  storyId: string,
  sceneId: string,
  payload: { role: SceneRole; model_id?: string; seed?: number | null },
): Promise<{ job_id: string; status: string }> {
  return requestJson<{ job_id: string; status: string }>(
    `/stories/${storyId}/scenes/${sceneId}/generate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        role: payload.role,
        model_id: payload.model_id ?? "",
        seed: payload.seed ?? null,
      }),
    },
  );
}

export function assembleStory(
  storyId: string,
  payload: { width?: number; height?: number; fps?: number } = {},
): Promise<{ job_id: string; status: string }> {
  return requestJson<{ job_id: string; status: string }>(
    `/stories/${storyId}/assemble`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        width: payload.width ?? 1920,
        height: payload.height ?? 1080,
        fps: payload.fps ?? 30,
      }),
    },
  );
}

/** Which roles a scene still needs, in production order. */
export function missingRolesForScene(
  detail: StoryDetail | null,
  sceneId: string,
): SceneRole[] {
  if (!detail) {
    return [];
  }
  const order: SceneRole[] = ["visual", "narration", "music"];
  const missing = detail.missing_assets
    .filter((entry) => entry.scene_id === sceneId)
    .map((entry) => entry.role as SceneRole);
  return order.filter((role) => missing.includes(role));
}

/** True when every scene has what the timeline needs. */
export function isReadyToAssemble(detail: StoryDetail | null): boolean {
  return Boolean(
    detail && detail.story.scenes.length > 0 && detail.missing_assets.length === 0,
  );
}
