import { useEffect, useState } from "react";

const contentCache = new Map<string, string>();
const inFlight = new Map<string, Promise<string>>();

/**
 * Fetch and cache a text asset's raw content from the `/outputs` static
 * server. Gallery thumbnails and the detail view both read the same asset, so
 * a module-level cache keeps a batch of cards from re-fetching identical URLs.
 */
export function useTextAssetContent(src: string | null): {
  content: string | null;
  isLoading: boolean;
} {
  const [content, setContent] = useState<string | null>(src ? contentCache.get(src) ?? null : null);
  const [isLoading, setIsLoading] = useState(Boolean(src) && !contentCache.has(src ?? ""));

  useEffect(() => {
    if (!src) {
      setContent(null);
      setIsLoading(false);
      return;
    }
    const cached = contentCache.get(src);
    if (cached !== undefined) {
      setContent(cached);
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    setIsLoading(true);
    let fetchPromise = inFlight.get(src);
    if (!fetchPromise) {
      fetchPromise = fetch(src)
        .then((response) => (response.ok ? response.text() : Promise.reject(new Error(`HTTP ${response.status}`))))
        .then((text) => {
          contentCache.set(src, text);
          return text;
        })
        .finally(() => {
          inFlight.delete(src);
        });
      inFlight.set(src, fetchPromise);
    }

    fetchPromise
      .then((text) => {
        if (!cancelled) {
          setContent(text);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setContent(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [src]);

  return { content, isLoading };
}

/** Strip common markdown syntax down to a plain-text thumbnail excerpt. */
export function excerptFromMarkdown(text: string, maxChars = 160): string {
  const plain = text
    .split("\n")
    .map((line) => line.replace(/^#{1,6}\s*/, "").replace(/^\s*[-*]\s*/, "").replace(/^\s*\d+\.\s*/, ""))
    .join(" ")
    .replace(/[*_`]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (plain.length <= maxChars) {
    return plain;
  }
  return `${plain.slice(0, maxChars - 1).trimEnd()}…`;
}
