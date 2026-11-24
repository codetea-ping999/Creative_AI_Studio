import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SetupPanel } from "./SetupPanel";
import type { ModelOption } from "./promptFormTypes";

const missingSsd: ModelOption = {
  id: "ssd-1b",
  displayName: "SSD-1B Local",
  defaultParams: {},
  tags: ["image"],
  isAvailable: false,
  isDefault: false,
  runtimeStatus: "missing_files",
  availabilityMessage: "Diffusers model files are missing: model_index.json",
  installPath: "/Users/me/Studio/models/image/ssd-1b",
  supportsLocalInstall: true,
  supportsNegativePrompt: true,
};

function renderPanel(
  onInstallLocalModel?: Parameters<typeof SetupPanel>[0]["onInstallLocalModel"],
  model: ModelOption = missingSsd,
) {
  return render(
    <SetupPanel
      modelsByMedia={{ image: [model], audio: [], video: [] }}
      readinessByMedia={{ image: "missing", audio: "missing", video: "missing" }}
      metrics={null}
      galleryStats={null}
      onRetry={() => {}}
      isRetrying={false}
      onInstallLocalModel={onInstallLocalModel}
    />,
  );
}

function openMissing() {
  const details = document.querySelector("details");
  if (details) {
    details.open = true;
  }
}

describe("SetupPanel local model install", () => {
  afterEach(cleanup);

  it("shows the download link and the install location", () => {
    renderPanel();
    openMissing();

    expect(screen.getByRole("link", { name: "取得先を開く" }).getAttribute("href")).toBe(
      "https://huggingface.co/segmind/SSD-1B",
    );
    expect(screen.getByText("/Users/me/Studio/models/image/ssd-1b")).toBeTruthy();
  });

  it("picks a folder for the right media and reports the outcome", async () => {
    const user = userEvent.setup();
    const install = vi.fn().mockResolvedValue({ status: "installed", message: "配置しました。" });
    renderPanel(install);
    openMissing();

    await user.click(screen.getByRole("button", { name: "手元のモデルを配置…" }));

    expect(install).toHaveBeenCalledWith("image", "ssd-1b", undefined);
    expect((await screen.findByRole("status")).textContent).toContain("配置しました。");
  });

  it("stays quiet when the folder dialog is cancelled", async () => {
    const user = userEvent.setup();
    const install = vi.fn().mockResolvedValue({ status: "cancelled" });
    renderPanel(install);
    openMissing();

    await user.click(screen.getByRole("button", { name: "手元のモデルを配置…" }));

    await waitFor(() => {
      expect(
        (screen.getByRole("button", { name: "手元のモデルを配置…" }) as HTMLButtonElement).disabled,
      ).toBe(false);
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("asks before replacing and then retries with replace", async () => {
    const user = userEvent.setup();
    const install = vi
      .fn()
      .mockResolvedValueOnce({
        status: "needs_replace",
        message: "配置先にファイルがあります。",
        sourcePath: "/Users/me/Downloads/ssd-1b",
      })
      .mockResolvedValueOnce({ status: "installed", message: "置き換えました。" });
    renderPanel(install);
    openMissing();

    await user.click(screen.getByRole("button", { name: "手元のモデルを配置…" }));
    expect((await screen.findByRole("alert")).textContent).toContain("配置先にファイルがあります。");
    await user.click(screen.getByRole("button", { name: "置き換えて配置" }));

    expect(install).toHaveBeenLastCalledWith("image", "ssd-1b", {
      sourcePath: "/Users/me/Downloads/ssd-1b",
      replace: true,
    });
    expect((await screen.findByRole("status")).textContent).toContain("置き換えました。");
  });

  it("shows an error and keeps the button usable", async () => {
    const user = userEvent.setup();
    const install = vi.fn().mockResolvedValue({ status: "error", message: "not a complete model" });
    renderPanel(install);
    openMissing();

    await user.click(screen.getByRole("button", { name: "手元のモデルを配置…" }));

    expect((await screen.findByRole("alert")).textContent).toContain("not a complete model");
    expect(
      (screen.getByRole("button", { name: "手元のモデルを配置…" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("hides the button without a handler or for non-installable models", () => {
    const { unmount } = renderPanel(undefined);
    openMissing();
    expect(screen.queryByRole("button", { name: "手元のモデルを配置…" })).toBeNull();
    unmount();

    renderPanel(vi.fn(), { ...missingSsd, supportsLocalInstall: false });
    openMissing();
    expect(screen.queryByRole("button", { name: "手元のモデルを配置…" })).toBeNull();
  });

  it("warns to place only trusted models next to the install button", () => {
    const trust = "信頼できる配布元から入手したモデルだけを配置してください。";
    const { unmount } = renderPanel(vi.fn());
    openMissing();
    expect(screen.getByText(trust)).toBeTruthy();
    unmount();

    renderPanel(vi.fn(), { ...missingSsd, supportsLocalInstall: false });
    openMissing();
    expect(screen.queryByText(trust)).toBeNull();
  });

  it("shows why the folder chooser failed", async () => {
    const user = userEvent.setup();
    const install = vi.fn().mockResolvedValue({
      status: "error",
      message: "macOS did not allow the API to open the folder chooser. Allow it in System Settings.",
    });
    renderPanel(install);
    openMissing();

    await user.click(screen.getByRole("button", { name: "手元のモデルを配置…" }));

    expect((await screen.findByRole("alert")).textContent).toContain("System Settings");
  });
});
