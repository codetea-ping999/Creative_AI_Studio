/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/main.tsx"],
      // Floor set to the measured baseline (2026-08-14) so the gate catches
      // regressions without blocking on pre-existing gaps (see issue #3,
      // App.tsx has 0% coverage and is the largest single drag on this number).
      thresholds: {
        statements: 40,
        branches: 44,
        functions: 41,
        lines: 40,
      },
    },
  },
});
