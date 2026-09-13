export type BuildEnvironment = Record<string, string | undefined>

const API_PROTOCOLS = new Set(['http:', 'https:'])

/** Validate the public API origin consumed by the browser bundle. */
export function validateApiOrigin(value: string | undefined): string {
  const configured = value?.trim()
  if (!configured) {
    throw new Error('VITE_API_URL is required for production builds')
  }

  let url: URL
  try {
    url = new URL(configured)
  } catch {
    throw new Error('VITE_API_URL must be an absolute HTTP(S) origin')
  }

  if (
    !API_PROTOCOLS.has(url.protocol) ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash
  ) {
    throw new Error('VITE_API_URL must be an absolute HTTP(S) origin')
  }

  return url.origin
}

export function reportsEnabled(value: string | undefined): boolean {
  return value?.trim().toLowerCase() !== 'false'
}

/** Fail a production build before it can produce an unsafe browser artifact. */
export function validateProductionBuildEnvironment(environment: BuildEnvironment): void {
  validateApiOrigin(environment.VITE_API_URL)
  if (
    reportsEnabled(environment.VITE_ENABLE_REPORTS) &&
    !environment.VITE_TURNSTILE_SITE_KEY?.trim()
  ) {
    throw new Error('VITE_TURNSTILE_SITE_KEY is required when VITE_ENABLE_REPORTS is enabled')
  }
}
