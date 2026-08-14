import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config([
  { ignores: ["dist", "coverage"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat["recommended-latest"],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // react-hooks 7's recommended set errors on every synchronous setState
      // inside useEffect, including the "reset local state when a selected
      // item changes" pattern used throughout this codebase. Downgraded to a
      // warning (staged adoption, matching the mypy rollout in this same
      // issue) rather than refactoring every call site as a side effect of
      // adding the gate.
      "react-hooks/set-state-in-effect": "warn",
    },
  },
]);
