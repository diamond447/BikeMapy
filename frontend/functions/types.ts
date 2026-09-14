/** Minimal Cloudflare Pages Function types kept local to avoid shipping a runtime dependency. */
export type PagesEnvironment = {
  /** Public read-only API origin. VITE_API_URL is also accepted for existing deployments. */
  PUBLIC_API_ORIGIN?: string
  VITE_API_URL?: string
  /** Canonical site origin. The request origin is used for previews when omitted. */
  PUBLIC_SITE_URL?: string
  VITE_PUBLIC_SITE_URL?: string
}

export type PagesContext = {
  request: Request
  env: PagesEnvironment
  next: (input?: Request | string, init?: ResponseInit) => Promise<Response>
}
