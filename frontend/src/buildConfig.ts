export type BuildEnvironment = Record<string, string | undefined>

const API_PROTOCOL = 'https:'

function parseIpv4Address(address: string): number[] | null {
  const octets = address.split('.')
  if (
    octets.length !== 4 ||
    octets.some((octet) => !/^\d{1,3}$/.test(octet) || Number(octet) > 255)
  ) {
    return null
  }
  return octets.map(Number)
}

function parseIpv6Address(address: string): number[] | null {
  const dottedParts = address.split(':').filter((part) => part.includes('.'))
  if (dottedParts.length > 1 || (dottedParts.length === 1 && !address.endsWith(dottedParts[0]))) {
    return null
  }

  const compression = address.indexOf('::')
  if (compression !== -1 && compression !== address.lastIndexOf('::')) return null

  const parsePart = (part: string): number[] | null => {
    if (!part) return []
    const pieces = part.split(':')
    const values: number[] = []
    for (const piece of pieces) {
      if (piece.includes('.')) {
        const octets = parseIpv4Address(piece)
        if (!octets || piece !== pieces[pieces.length - 1]) return null
        values.push((octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3])
      } else {
        if (!/^[\da-f]{1,4}$/i.test(piece)) return null
        values.push(Number.parseInt(piece, 16))
      }
    }
    return values
  }

  if (compression === -1) {
    const values = parsePart(address)
    return values?.length === 8 ? values : null
  }

  const left = parsePart(address.slice(0, compression))
  const right = parsePart(address.slice(compression + 2))
  if (!left || !right || left.length + right.length >= 8) return null
  return [...left, ...Array(8 - left.length - right.length).fill(0), ...right]
}

function isNonPublicIpv4Address(octets: number[]): boolean {
  const [first, second] = octets
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && (second === 0 || second === 2 || second === 168)) ||
    (first === 192 && second === 88 && octets[2] === 99) ||
    (first === 198 && (second === 18 || second === 19 || second === 51)) ||
    (first === 203 && second === 0 && octets[2] === 113) ||
    first >= 224
  )
}

function isNonPublicIpv6Address(parts: number[]): boolean {
  const first = parts[0]
  const mappedIpv4 = parts.slice(0, 5).every((part) => part === 0) && parts[5] === 0xffff
  const compatibleIpv4 = parts.slice(0, 6).every((part) => part === 0)
  if (mappedIpv4) {
    const octets = [parts[6] >> 8, parts[6] & 0xff, parts[7] >> 8, parts[7] & 0xff]
    return isNonPublicIpv4Address(octets)
  }
  if (compatibleIpv4) {
    return true
  }

  if (
    first === 0 ||
    (first & 0xfe00) === 0xfc00 ||
    (first & 0xffc0) === 0xfe80 ||
    (first & 0xffc0) === 0xfec0 ||
    (first & 0xff00) === 0xff00 ||
    (first === 0x2001 && parts[1] === 0x0db8)
  ) {
    return true
  }
  return false
}

function isNonPublicHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (
    normalized === 'localhost' ||
    normalized.endsWith('.localhost') ||
    normalized.endsWith('.local')
  ) {
    return true
  }

  if (normalized.includes(':')) {
    const parts = parseIpv6Address(normalized)
    return !parts || isNonPublicIpv6Address(parts)
  }

  const octets = parseIpv4Address(normalized)
  return octets ? isNonPublicIpv4Address(octets) : false
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
