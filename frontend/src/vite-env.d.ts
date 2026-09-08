/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Public token for the optional cookie-free Cloudflare Web Analytics beacon. */
  readonly VITE_CF_WEB_ANALYTICS_TOKEN?: string
  readonly VITE_API_URL?: string
  /** Set to false for read-only Cloudflare pull-request previews. */
  readonly VITE_ENABLE_REPORTS?: string
}
