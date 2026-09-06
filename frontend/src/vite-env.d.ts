/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Set to false for read-only Cloudflare pull-request previews. */
  readonly VITE_ENABLE_REPORTS?: string
}
