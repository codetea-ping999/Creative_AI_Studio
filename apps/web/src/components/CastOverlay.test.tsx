import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CastOverlay } from "./CastOverlay";

describe("CastOverlay", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("shows a decorative cut-in and clears itself", () => {
    vi.useFakeTimers();
    const onDone = vi.fn();
    const { container } = render(<CastOverlay cue={{ id: 1, kind: "complete" }} onDone={onDone} />);

    const overlay = container.querySelector(".cast-overlay");
    expect(overlay?.getAttribute("aria-hidden")).toBe("true");
    expect(overlay?.textContent).toContain("完成！");

    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("names the new level and title on a level up", () => {
    const { container } = render(
      <CastOverlay cue={{ id: 2, kind: "levelup", level: 2, title: "駆け出しの演出家" }} onDone={() => {}} />,
    );
    expect(container.textContent).toContain("Lv 2・駆け出しの演出家");
  });

  it("stays hidden when the device asks for reduced motion", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce") }));
    const { container } = render(<CastOverlay cue={{ id: 3, kind: "start" }} onDone={() => {}} />);
    expect(container.querySelector(".cast-overlay")).toBeNull();
  });
});
