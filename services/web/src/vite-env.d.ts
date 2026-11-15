/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the downloader API, no trailing slash. Empty (the compose build) means same origin through nginx. */
  readonly VITE_API_BASE_URL: string;
  /**
   * `X-Api-Key` sent on every request. This is an internal tool for 1-3
   * operators (plan.md phase-08 Risks table) baked at build/deploy time via
   * compose env, not entered through any UI -- there is deliberately no
   * cookie-paste-style input box for it either (phase-08 §8.5 applies the
   * same reasoning: don't build UI for a value that flows through env/curl).
   */
  readonly VITE_API_KEY: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
