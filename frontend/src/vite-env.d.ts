/// <reference types="vite/client" />

// Task 2.7 — declared so import.meta.env.VITE_SENTRY_DSN is typed rather
// than `any`. Optional: builds without a DSN are the normal case and are
// meant to leave Sentry dormant, not fail.
interface ImportMetaEnv {
  readonly VITE_SENTRY_DSN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
