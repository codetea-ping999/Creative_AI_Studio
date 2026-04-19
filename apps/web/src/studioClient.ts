import { API_BASE_URL } from "./studio";
import type {
  CreateFeedbackPayload,
  CreateJobResponse,
  ExportAssetResponse,
  ExportProjectResponse,
  FeedbackResponse,
  GalleryAssetDetailResponse,
  GalleryItemResponse,
  JobResponse,
  LoraCatalogResponse,
  MetricsSummaryResponse,
  ModelsResponse,
  ProjectJobsResponse,
  ProjectResponse,
  ReuseAssetResponse,
} from "./studio";
import type { MediaType } from "./components/promptFormTypes";

export type ProjectListFilters = {
  query?: string;
  status?: string;
  tag?: string;
};

type JsonInit = Omit<RequestInit, "body"> & {
  body?: unknown;
};

async function requestJson<T>(path: string, init: JsonInit = {}): Promise<T> {
  const { body, headers, ...rest } = init;
  const requestHeaders = new Headers(headers);
  let requestBody: BodyInit | undefined;

  if (body !== undefined) {
    if (
      typeof body === "string" ||
      body instanceof Blob ||
      body instanceof FormData ||
      body instanceof URLSearchParams
    ) {
      requestBody = body;
    } else {
      if (!requestHeaders.has("Content-Type")) {
        requestHeaders.set("Content-Type", "application/json");
      }
      requestBody = JSON.stringify(body);
    }
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...rest,
    headers: requestHeaders,
    body: requestBody,
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}

function buildProjectListPath(filters: ProjectListFilters = {}): string {
  const params = new URLSearchParams();

  if (filters.query) {
    params.set("q", filters.query);
  }
  if (filters.status) {
    params.set("status", filters.status);
  }
  if (filters.tag) {
    params.set("tag", filters.tag);
  }

  const query = params.toString();
  return query ? `/projects?${query}` : "/projects";
}

export const studioClient = {
  listProjects(filters: ProjectListFilters = {}): Promise<ProjectResponse[]> {
    return requestJson<ProjectResponse[]>(buildProjectListPath(filters));
  },

  listModels(mediaType: MediaType): Promise<ModelsResponse> {
    return requestJson<ModelsResponse>(`/models?media_type=${encodeURIComponent(mediaType)}`);
  },

  listLoras(): Promise<LoraCatalogResponse> {
    return requestJson<LoraCatalogResponse>("/catalog/loras");
  },

  getProjectWorkspace(projectId: string): Promise<ProjectJobsResponse> {
    return requestJson<ProjectJobsResponse>(`/projects/${projectId}/jobs`);
  },

  getMetricsSummary(): Promise<MetricsSummaryResponse> {
    return requestJson<MetricsSummaryResponse>("/metrics/summary");
  },

  listGallery(mediaType: MediaType, limit: number): Promise<GalleryItemResponse[]> {
    return requestJson<GalleryItemResponse[]>(
      `/gallery?media_type=${encodeURIComponent(mediaType)}&limit=${limit}`,
    );
  },

  getGalleryAssetDetail(assetId: string): Promise<GalleryAssetDetailResponse> {
    return requestJson<GalleryAssetDetailResponse>(`/gallery/${assetId}`);
  },

  getGalleryAssetDetailByJob(jobId: string): Promise<GalleryAssetDetailResponse> {
    return requestJson<GalleryAssetDetailResponse>(`/gallery/job/${jobId}`);
  },

  getJob(jobId: string): Promise<JobResponse> {
    return requestJson<JobResponse>(`/jobs/${jobId}`);
  },

  createGenerationJob(
    mediaType: MediaType,
    payload: Record<string, unknown>,
  ): Promise<CreateJobResponse> {
    return requestJson<CreateJobResponse>(`/generate/${mediaType}`, {
      method: "POST",
      body: payload,
    });
  },

  reuseAsset(
    assetId: string,
    payload: Record<string, unknown>,
  ): Promise<ReuseAssetResponse> {
    return requestJson<ReuseAssetResponse>(`/gallery/${assetId}/reuse`, {
      method: "POST",
      body: payload,
    });
  },

  exportAsset(assetId: string): Promise<ExportAssetResponse> {
    return requestJson<ExportAssetResponse>(`/gallery/${assetId}/export`, {
      method: "POST",
      body: { include_metadata: true },
    });
  },

  updateAssetProject(
    assetId: string,
    projectId: string | null,
  ): Promise<GalleryAssetDetailResponse> {
    return requestJson<GalleryAssetDetailResponse>(`/gallery/${assetId}/project`, {
      method: "PATCH",
      body: { project_id: projectId },
    });
  },

  createProject(payload: Record<string, unknown>): Promise<ProjectResponse> {
    return requestJson<ProjectResponse>("/projects", {
      method: "POST",
      body: payload,
    });
  },

  updateProject(
    projectId: string,
    payload: Record<string, unknown>,
  ): Promise<ProjectResponse> {
    return requestJson<ProjectResponse>(`/projects/${projectId}`, {
      method: "PATCH",
      body: payload,
    });
  },

  exportProject(projectId: string): Promise<ExportProjectResponse> {
    return requestJson<ExportProjectResponse>(`/projects/${projectId}/export`, {
      method: "POST",
      body: {},
    });
  },

  submitFeedback(payload: CreateFeedbackPayload): Promise<FeedbackResponse> {
    return requestJson<FeedbackResponse>("/feedback", {
      method: "POST",
      body: payload,
    });
  },
};
