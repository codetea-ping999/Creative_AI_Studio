import { createContext, useCallback, useContext } from "react";
import { en } from "./en";

/**
 * Japanese is the source language: every UI string is written in Japanese
 * and used as its own key, so a missing English entry falls back to the
 * Japanese text instead of showing a raw key.
 */
export type Locale = "ja" | "en";

export type TranslateVars = Record<string, string | number>;

export type Translate = (text: string, vars?: TranslateVars) => string;

export const LOCALE_STORAGE_KEY = "creative-ai-studio.locale";

export const supportedLocales: readonly Locale[] = ["ja", "en"];

export const localeLabels: Record<Locale, string> = {
  ja: "日本語",
  en: "English",
};

function interpolate(text: string, vars?: TranslateVars): string {
  if (!vars) {
    return text;
  }
  return text.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}

export function translate(locale: Locale, text: string, vars?: TranslateVars): string {
  const source = locale === "en" ? (en[text] ?? text) : text;
  return interpolate(source, vars);
}

export type LocaleContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
};

export const LocaleContext = createContext<LocaleContextValue>({
  locale: "ja",
  setLocale: () => {},
});

export function useLocale(): LocaleContextValue {
  return useContext(LocaleContext);
}

export function useT(): Translate {
  const { locale } = useContext(LocaleContext);
  return useCallback(
    (text: string, vars?: TranslateVars) => translate(locale, text, vars),
    [locale],
  );
}

export function readStoredLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return stored === "en" || stored === "ja" ? stored : "ja";
  } catch {
    return "ja";
  }
}
