/**
 * Studio themes. Ten palettes run the playful "魔法工学" layer (cut-ins,
 * spell-circle progress); the plain light and dark themes stay quiet.
 * Names and keywords are Japanese source text; English lives in i18n/en.ts.
 */
export type PaletteId =
  | "shiden"
  | "guren"
  | "kokkin"
  | "shuya"
  | "sakura"
  | "sokyoku"
  | "hisui"
  | "raimei"
  | "hakuai"
  | "geppaku";

export type ThemeChoice = PaletteId | "light" | "dark";

export type PaletteInfo = {
  id: PaletteId;
  name: string;
  keywords: string;
  mode: "dark" | "light";
};

export const palettes: readonly PaletteInfo[] = [
  { id: "shiden", name: "紫電", keywords: "神秘・魔法・創造性", mode: "dark" },
  { id: "guren", name: "紅蓮", keywords: "情熱・勇気・興奮", mode: "dark" },
  { id: "kokkin", name: "黒金", keywords: "格・達成・特別感", mode: "dark" },
  { id: "shuya", name: "朱夜", keywords: "信頼と活力・和の力強さ", mode: "dark" },
  { id: "sakura", name: "桜墨", keywords: "華やかさ・遊び心", mode: "dark" },
  { id: "sokyoku", name: "双極", keywords: "対比・変身・躍動感", mode: "dark" },
  { id: "hisui", name: "翡翠", keywords: "集中・成長・テクノロジー", mode: "dark" },
  { id: "raimei", name: "雷鳴", keywords: "注目・楽観・スピード", mode: "dark" },
  { id: "hakuai", name: "白藍", keywords: "清潔・知的・和の上品さ", mode: "light" },
  { id: "geppaku", name: "月白", keywords: "明快・信頼・モダン", mode: "light" },
];

export const DEFAULT_THEME: ThemeChoice = "shiden";

const paletteIds = new Set<string>(palettes.map((palette) => palette.id));

export function isPaletteId(value: string): value is PaletteId {
  return paletteIds.has(value);
}

/** Reads a stored choice; the first HUD release stored "hud" (= 紫電). */
export function parseThemeChoice(value: string | null): ThemeChoice {
  if (value === "light" || value === "dark") {
    return value;
  }
  if (value === "hud") {
    return "shiden";
  }
  return value && isPaletteId(value) ? value : DEFAULT_THEME;
}

/** The root attributes a choice needs: base light/dark plus an optional palette. */
export function themeAttributes(choice: ThemeChoice): {
  theme: "light" | "dark";
  palette: PaletteId | null;
} {
  if (choice === "light" || choice === "dark") {
    return { theme: choice, palette: null };
  }
  const palette = palettes.find((entry) => entry.id === choice);
  return { theme: palette?.mode ?? "dark", palette: choice };
}
