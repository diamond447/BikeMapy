export type BuildEnvironment = Record<string, string | undefined>

const API_PROTOCOL = 'https:'

function isNonPublicHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (
    normalized === 'localhost' ||
    normalized.endsWith('.localhost') ||
    normalized.endsWith('.local') ||
    normalized === '0.0.0.0' ||
    normalized === '::' ||
    normalized === '::1' ||
    normalized.startsWith('fc') ||
    normalized.startsWith('fd') ||
    normalized.startsWith('fe80:')
  ) {
    return true
  }

  const octets = normalized.split('.').map(Number)
  if (octets.length !== 4 || octets.some((octet) => !Number.isInteger(octet))) return false
  const [first, second] = octets
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168)
  )
}

/** Validate the public HTTPS API origin consumed by a deployable bundle. */
export function validateApiOrigin(value: string | undefined): string {
  const configured = value?.trim()
  if (!configured) {
    throw new Error('VITE_API_URL is required for production builds')
  }

  let url: URL
  try {
    url = new URL(configured)
  } catch {
    throw new Error('VITE_API_URL must be an absolute public HTTPS origin')
  }

  if (
    url.protocol !== API_PROTOCOL ||
    isNonPublicHostname(url.hostname) ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash
  ) {
    throw new Error('VITE_API_URL must be an absolute public HTTPS origin')
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
