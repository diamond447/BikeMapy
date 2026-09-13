export type BuildEnvironment = Record<string, string | undefined>

const API_PROTOCOL = 'https:'

function isInvalidApiHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (
    normalized === 'localhost' ||
    normalized.endsWith('.localhost') ||
    normalized.endsWith('.local') ||
    normalized.endsWith('.internal') ||
    normalized.endsWith('.intranet') ||
    normalized.endsWith('.lan') ||
    normalized.endsWith('.home.arpa')
  ) {
    return true
  }

  // URL canonicalization turns IPv4, IPv6, and IPv4-mapped IPv6 literals into
  // stable forms. Reject every literal instead of maintaining an incomplete
  // list of private and special-use ranges.
  if (normalized.includes(':') || /^(?:\d{1,3}\.){3}\d{1,3}$/.test(normalized)) {
    return true
  }

  const dnsName = normalized.endsWith('.') ? normalized.slice(0, -1) : normalized
  const labels = dnsName.split('.')
  return (
    labels.length < 2 || labels.some((label) => !/^[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?$/i.test(label))
  )
}

/** Validate the public HTTPS DNS origin consumed by a deployable bundle. */
export function validateApiOrigin(value: string | undefined): string {
  const configured = value?.trim()
  if (!configured) {
    throw new Error('VITE_API_URL is required for production builds')
  }

  let url: URL
  try {
    url = new URL(configured)
  } catch {
    throw new Error('VITE_API_URL must be an absolute public HTTPS DNS origin')
  }

  if (
    url.protocol !== API_PROTOCOL ||
    isInvalidApiHostname(url.hostname) ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash
  ) {
    throw new Error('VITE_API_URL must be an absolute public HTTPS DNS origin')
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
