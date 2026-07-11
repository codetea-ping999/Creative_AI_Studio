const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";

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
  const response = await fetch(`${API_BASE_URL}${path}`, init);
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
  if (relativePath.startsWith("outputs/")) {
    return `${API_BASE_URL}/${relativePath}`;
  }

  const outputMarker = "/outputs/";
  const outputMarkerIndex = normalized.lastIndexOf(outputMarker);
  if (outputMarkerIndex >= 0) {
    return `${API_BASE_URL}${normalized.slice(outputMarkerIndex)}`;
  }

  for (const mountChild of ["images", "audio", "videos", "exports"]) {
    if (relativePath.startsWith(`${mountChild}/`)) {
      return `${API_BASE_URL}/outputs/${relativePath}`;
    }

    const childMarker = `/${mountChild}/`;
    const childMarkerIndex = normalized.lastIndexOf(childMarker);
    if (childMarkerIndex >= 0) {
      return `${API_BASE_URL}/outputs${normalized.slice(childMarkerIndex)}`;
    }
  }

  return null;
}
