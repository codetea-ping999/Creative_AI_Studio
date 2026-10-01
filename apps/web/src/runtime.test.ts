import { afterEach, describe, expect, it, vi } from "vitest";

import { BrowserRuntime, DesktopRuntime, detectStudioRuntime } from "./runtime";

describe("StudioRuntime", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it("BrowserRuntime falls back to the loopback endpoint", async () => {
    const runtime = new BrowserRuntime();
    expect(runtime.name).toBe("browser");
    await expect(runtime.resolveBackendBaseUrl()).resolves.toBe("http://127.0.0.1:8000");
  });

  it("BrowserRuntime honors VITE_API_BASE_URL and trims a trailing slash", async () => {
    const previous = import.meta.env.VITE_API_BASE_URL;
    import.meta.env.VITE_API_BASE_URL = "http://127.0.0.1:8123/";
    try {
      const runtime = new BrowserRuntime();
      await expect(runtime.resolveBackendBaseUrl()).resolves.toBe("http://127.0.0.1:8123");
    } finally {
      if (previous === undefined) {
        delete import.meta.env.VITE_API_BASE_URL;
      } else {
        import.meta.env.VITE_API_BASE_URL = previous;
      }
    }
  });

  it("DesktopRuntime resolves the endpoint over Tauri IPC", async () => {
    window.__TAURI_INTERNALS__ = {
      invoke: vi.fn().mockResolvedValue("http://127.0.0.1:8123/"),
    };
    const runtime = new DesktopRuntime();
    expect(runtime.name).toBe("desktop");
    await expect(runtime.resolveBackendBaseUrl()).resolves.toBe("http://127.0.0.1:8123");
  });

  it("DesktopRuntime falls back to the loopback endpoint without IPC", async () => {
    window.__TAURI_INTERNALS__ = {};
    const runtime = new DesktopRuntime();
    await expect(runtime.resolveBackendBaseUrl()).resolves.toBe("http://127.0.0.1:8000");
  });

  it("detectStudioRuntime selects DesktopRuntime when Tauri IPC is present", () => {
    window.__TAURI_INTERNALS__ = { invoke: vi.fn() };
    expect(detectStudioRuntime().name).toBe("desktop");
  });

  it("detectStudioRuntime selects BrowserRuntime without Tauri IPC", () => {
    expect(detectStudioRuntime().name).toBe("browser");
  });
});