/**
 * Backend endpoint resolution for the Studio UI.
 *
 * ADR `docs/desktop/architecture-decision.md` ¶3 requires the packaged desktop
 * bundle to not depend solely on build-time `VITE_API_BASE_URL`. This boundary
 * selects a runtime strategy so React features never touch the renderer
 * transport directly:
 *
 * - `BrowserRuntime` resolves from `VITE_API_BASE_URL` with the loopback
 *   default as the final fallback (the existing browser behavior);
 * - `DesktopRuntime` resolves the endpoint from the Tauri shell over IPC, so
 *   a supported non-default backend port stays usable without rebuilding.
 */

const LOOPBACK_FALLBACK = "http://127.0.0.1:8000";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: {
      invoke?: <T = unknown>(command: string, args?: Record<string, unknown>) => Promise<T>;
    };
  }
}

export interface StudioRuntime {
  name: "browser" | "desktop";
  resolveBackendBaseUrl(): Promise<string>;
}

function trimTrailingSlash(value: string): string {
  return value.endsWith("/") ? value.slice(0, -1) : value;
}

export class BrowserRuntime implements StudioRuntime {
  readonly name = "browser" as const;

  /** Synchronous browser resolution; used so `requestJson` never needs to
   * await an endpoint in the browser path (preserves existing fetch timing). */
  configuredBaseUrl(): string {
    const configured = import.meta.env.VITE_API_BASE_URL as string | undefined;
    return trimTrailingSlash(configured ?? LOOPBACK_FALLBACK);
  }

  async resolveBackendBaseUrl(): Promise<string> {
    return this.configuredBaseUrl();
  }
}

export class DesktopRuntime implements StudioRuntime {
  readonly name = "desktop" as const;

  async resolveBackendBaseUrl(): Promise<string> {
    const invoke = window.__TAURI_INTERNALS__?.invoke;
    if (typeof invoke === "function") {
      const resolved = await invoke<string>("get_backend_endpoint");
      if (typeof resolved === "string" && resolved) {
        return trimTrailingSlash(resolved);
      }
    }
    return LOOPBACK_FALLBACK;
  }
}

export function detectStudioRuntime(): StudioRuntime {
  if (typeof window !== "undefined" && window.__TAURI_INTERNALS__?.invoke) {
    return new DesktopRuntime();
  }
  return new BrowserRuntime();
}