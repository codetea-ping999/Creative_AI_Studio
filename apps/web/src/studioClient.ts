import { detectStudioRuntime, type StudioRuntime } from "./runtime";

const DEFAULT_LOOPBACK_BASE_URL = "http://127.0.0.1:8000";

/**
 * Resolve the active runtime once at module load. The browser endpoint is known
 * synchronously (build-time env / loopback fallback), so `requestJson` keeps
 * exactly the same fetch timing as the pre-desktop implementation. The desktop
 * endpoint comes from a cached Tauri IPC call and is awaited on first request.
 */
const activeRuntime: StudioRuntime = detectStudioRuntime();

let resolvedBaseUrl: string | null = null;
let baseUrlPromise: Promise<string> | null = null;

const isBrowserRuntime = activeRuntime.name === "browser";

if (isBrowserRuntime) {
  const browserRuntime = activeRuntime as import("./runtime").BrowserRuntime;
  resolvedBaseUrl = browserRuntime.configuredBaseUrl();
}

function resolveBackendBaseUrl(): Promise<string> {
  baseUrlPromise ??= activeRuntime
    .resolveBackendBaseUrl()
    .then((value) => {
      resolvedBaseUrl = value;
      return value;
    })
    .catch(() => {
      resolvedBaseUrl = DEFAULT_LOOPBACK_BASE_URL;
      return resolvedBaseUrl;
    });
  return baseUrlPromise;
}

function currentBaseUrlSync(): string {
  return resolvedBaseUrl ?? DEFAULT_LOOPBACK_BASE_URL;
}

export function formatApiErrorDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (!Array.isArray(detail)) {
    return "";
  }

  return detail
    .map((item) => {
      if (!item || typeof item !== "object") {
        return "";
      }
      const payload = item as { loc?: unknown; msg?: unknown };
      const loc = Array.isArray(payload.loc)
        ? payload.loc.map((part) => String(part)).join(".")
        : "";
      const message = typeof payload.msg === "string" ? payload.msg : "";
      return loc ? (message ? `${loc}: ${message}` : loc) : message;
    })
    .filter(Boolean)
    .join("; ");
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  // Browser fetch timing must stay identical to the pre-desktop
  // implementation: the endpoint is known synchronously and no IPC await is
  // inserted before `fetch`. The desktop path resolves (and caches) once.
  const base = isBrowserRuntime
    ? currentBaseUrlSync()
    : (resolvedBaseUrl ?? await resolveBackendBaseUrl());
  const response = await fetch(`${base}${path}`, init);
  const responseText = await response.text();
  if (!response.ok) {
    let detail = responseText;
    try {
      const parsed = JSON.parse(responseText) as { detail?: unknown };
      detail = formatApiErrorDetail(parsed.detail) || detail;
    } catch {
      // Keep the raw response text when the API does not return JSON.
    }
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return responseText ? (JSON.parse(responseText) as T) : (undefined as T);
}

export function createOutputUrl(pathValue: string | null | undefined): string | null {
  if (!pathValue) {
    return null;
  }

  const normalized = pathValue.replace(/\\/g, "/");
  if (/^https?:\/\//i.test(normalized)) {
    return normalized;
  }

  const relativePath = normalized.replace(/^\.?\//, "");
  const base = currentBaseUrlSync();

  for (const mountChild of ["images", "audio", "videos", "exports"]) {
    if (relativePath.startsWith(`${mountChild}/`)) {
      return `${base}/outputs/${relativePath}`;
    }

    const childMarker = `/${mountChild}/`;
    const childMarkerIndex = normalized.lastIndexOf(childMarker);
    if (childMarkerIndex >= 0) {
      return `${base}/outputs${normalized.slice(childMarkerIndex)}`;
    }
  }

  const outputMarker = "/outputs/";
  const outputMarkerIndex = normalized.lastIndexOf(outputMarker);
  if (outputMarkerIndex >= 0) {
    return `${base}${normalized.slice(outputMarkerIndex)}`;
  }

  if (relativePath.startsWith("outputs/")) {
    return `${base}/${relativePath}`;
  }

  return null;
}