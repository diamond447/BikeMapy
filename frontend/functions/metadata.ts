import { routeUrl, socialImageUrl } from '../src/siteMetadata'

export type PublicRoute = {
  id: string
  slug: string
  title: string
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export function isRouteId(value: string | null): value is string {
  return value !== null && UUID.test(value)
}

function normalizeOrigin(value: string | undefined, fallback: string): string {
  const candidate = value?.trim() || fallback
  const parsed = new URL(candidate)
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('An HTTP(S) origin is required')
  }
  if (parsed.username || parsed.password || (parsed.pathname !== '/' && parsed.pathname !== '')) {
    throw new Error('An origin must not contain credentials or a path')
  }
  return parsed.origin
}

export function apiOrigin(value: string | undefined): string {
  return normalizeOrigin(value, 'http://localhost:8000')
}

export function siteOrigin(value: string | undefined, requestUrl: string): string {
  return normalizeOrigin(value, new URL(requestUrl).origin)
}

export function routeApiUrl(origin: string, routeId: string): string {
  return `${apiOrigin(origin)}/api/v1/routes/${encodeURIComponent(routeId)}/`
}

function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character] ??
      character,
  )
}

function metadataTag(attribute: 'name' | 'property', name: string, content: string): string {
  return `<meta ${attribute}="${escapeHtml(name)}" content="${escapeHtml(content)}" />`
}

function removeMetadata(html: string): string {
  return html
    .replace(/<title\b[^>]*>[\s\S]*?<\/title>\s*/gi, '')
    .replace(
      /<meta\b[^>]*(?:name|property)=["'](?:description|og:[^"']+|twitter:[^"']+)["'][^>]*>\s*/gi,
      '',
    )
    .replace(/<link\b[^>]*rel=["']canonical["'][^>]*>\s*/gi, '')
}

/** Inject route metadata into the Vite document while preserving the SPA shell. */
export function renderRouteDocument(
  html: string,
  route: PublicRoute,
  publicOrigin: string,
): string {
  const title = `${route.title} · BikeMapy`
  const description = `${route.title}. Discover this cycling route and its source in BikeMapy.`
  const canonical = routeUrl(publicOrigin, route.id, route.slug)
  const image = socialImageUrl(publicOrigin)
  const tags = [
    `<title>${escapeHtml(title)}</title>`,
    metadataTag('name', 'description', description),
    metadataTag('property', 'og:title', title),
    metadataTag('property', 'og:description', description),
    metadataTag('property', 'og:type', 'article'),
    metadataTag('property', 'og:site_name', 'BikeMapy'),
    metadataTag('property', 'og:url', canonical),
    metadataTag('property', 'og:image', image),
    metadataTag('property', 'og:image:alt', `${route.title} — BikeMapy route preview`),
    metadataTag('property', 'og:image:type', 'image/png'),
    metadataTag('property', 'og:image:width', '1200'),
    metadataTag('property', 'og:image:height', '630'),
    metadataTag('name', 'twitter:card', 'summary_large_image'),
    metadataTag('name', 'twitter:title', title),
    metadataTag('name', 'twitter:description', description),
    metadataTag('name', 'twitter:image', image),
    `<link rel="canonical" href="${escapeHtml(canonical)}" />`,
  ].join('\n    ')
  const cleanHtml = removeMetadata(html)
  return cleanHtml.replace('</head>', `    ${tags}\n  </head>`)
}

/** Parse an API route response without trusting arbitrary fields in the document. */
export function parsePublicRoute(value: unknown, expectedId: string): PublicRoute | null {
  if (!value || typeof value !== 'object') return null
  const route = value as Record<string, unknown>
  if (
    typeof route.id !== 'string' ||
    route.id.toLowerCase() !== expectedId.toLowerCase() ||
    typeof route.slug !== 'string' ||
    !route.slug ||
    typeof route.title !== 'string' ||
    !route.title.trim()
  ) {
    return null
  }
  return { id: route.id, slug: route.slug, title: route.title.trim() }
}

export async function fetchPublicRoute(
  fetcher: typeof fetch,
  origin: string,
  routeId: string,
): Promise<PublicRoute | null> {
  const response = await fetcher(routeApiUrl(origin, routeId), {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) return null
  return parsePublicRoute(await response.json(), routeId)
}

export function routeIdFromRequest(requestUrl: string): string | null {
  return new URL(requestUrl).searchParams.get('route')
}

export function isRouteRequestPath(pathname: string): boolean {
  return pathname === '/' || pathname === ''
}
