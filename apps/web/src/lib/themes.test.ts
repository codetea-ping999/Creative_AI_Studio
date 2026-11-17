import { describe, expect, it } from "vitest";
import { en } from "../i18n/en";
import { palettes, parseThemeChoice, themeAttributes } from "./themes";

describe("themes", () => {
  it("defaults to 紫電 and migrates the first HUD value", () => {
    expect(parseThemeChoice(null)).toBe("shiden");
    expect(parseThemeChoice("hud")).toBe("shiden");
    expect(parseThemeChoice("nonsense")).toBe("shiden");
    expect(parseThemeChoice("light")).toBe("light");
    expect(parseThemeChoice("sakura")).toBe("sakura");
  });

  it("maps palettes to their base theme", () => {
    expect(themeAttributes("guren")).toEqual({ theme: "dark", palette: "guren" });
    expect(themeAttributes("hakuai")).toEqual({ theme: "light", palette: "hakuai" });
    expect(themeAttributes("dark")).toEqual({ theme: "dark", palette: null });
  });

  it("has English names and keywords for every palette", () => {
    const missing = palettes.flatMap((palette) => [palette.name, palette.keywords]).filter((text) => !(text in en));
    expect(missing).toEqual([]);
  });
});
