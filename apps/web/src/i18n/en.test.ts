import { describe, expect, it } from "vitest";
import { en } from "./en";
import { translate } from "./index";

// Every literal t("…") key in the app must have an English entry, so the
// English interface never falls back to Japanese for a string we wrote.
const sources = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}", "!./**"], {
  query: "?raw",
  import: "default",
  eager: true,
});

function literalKeys(source: string): string[] {
  const keys: string[] = [];
  for (const match of source.matchAll(/\bt\(\s*"((?:[^"\\]|\\.)*)"/g)) {
    keys.push(JSON.parse(`"${match[1]}"`) as string);
  }
  return keys;
}

describe("English dictionary", () => {
  it("covers every literal UI string", () => {
    const missing = new Set<string>();
    for (const source of Object.values(sources)) {
      for (const key of literalKeys(source)) {
        if (!(key in en)) {
          missing.add(key);
        }
      }
    }
    expect(Object.keys(sources).length).toBeGreaterThan(10);
    expect([...missing]).toEqual([]);
  });

  it("keeps the same placeholders as the Japanese source", () => {
    const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    const mismatched = Object.entries(en).filter(
      ([ja, english]) => placeholders(ja).join() !== placeholders(english).join(),
    );
    expect(mismatched).toEqual([]);
  });

  it("falls back to the Japanese text and fills placeholders", () => {
    expect(translate("en", "未登録の文言")).toBe("未登録の文言");
    expect(translate("ja", "シード {seed}", { seed: 7 })).toBe("シード 7");
    expect(translate("en", "シード {seed}", { seed: 7 })).toBe("Seed 7");
  });
});
