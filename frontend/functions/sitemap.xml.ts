import { apiOrigin, siteOrigin } from './metadata'
import type { PagesContext } from './types'

type RoutePage = {
  results?: Array<{ id?: unknown; slug?: unknown }>
  next?: string | null
}

const PAGE_SIZE = 100
const MAX_PAGES = 500

function routeUrl(origin: string, id: string, slug: string): string {
  const url = new URL('/', `${origin}/`)
  url.searchParams.set('route', id)
  url.searchParams.set('slug', slug)
  return url.href
}

function escapeXml(value: string): string {
  return value.replace(/[<>&"']/g, (character) => {
    const escaped: Record<string, string> = {
      '<': '&lt;',
      '>': '&gt;',
      '&': '&amp;',
      '"': '&quot;',
      "'": '&apos;',
    }
    return escaped[character] ?? character
  })
}

export function renderSitemap(origin: string, urls: string[]): string {
  const locations = [...new Set([`${origin}/`, ...urls])]
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ...locations.map((location) => `  <url><loc>${escapeXml(location)}</loc></url>`),
    '</urlset>',
    '',
  ].join('\n')
}

async function fetchRouteUrls(fetcher: typeof fetch, api: string, site: string): Promise<string[]> {
  const urls: string[] = []
  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const endpoint = new URL('/api/v1/routes/', `${api}/`)
    endpoint.searchParams.set('page', String(page))
    endpoint.searchParams.set('page_size', String(PAGE_SIZE))
    const response = await fetcher(endpoint, { headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`Route catalogue returned ${response.status}`)
    const payload = (await response.json()) as RoutePage
    for (const route of payload.results ?? []) {
      if (typeof route.id === 'string' && typeof route.slug === 'string' && route.slug) {
        urls.push(routeUrl(site, route.id, route.slug))
      }
    }
    if (!payload.next || (payload.results ?? []).length < PAGE_SIZE) break
  }
  return urls
}

export async function onRequest({ request, env, next }: PagesContext): Promise<Response> {
  if (request.method !== 'GET') return next()
  try {
    const site = siteOrigin(env.PUBLIC_SITE_URL ?? env.VITE_PUBLIC_SITE_URL, request.url)
    const urls = await fetchRouteUrls(
      fetch,
      apiOrigin(env.PUBLIC_API_ORIGIN ?? env.VITE_API_URL),
      site,
    )
    return new Response(renderSitemap(site, urls), {
      headers: {
        'cache-control': 'public, max-age=300, s-maxage=300',
        'content-type': 'application/xml; charset=UTF-8',
      },
    })
  } catch {
    // Keep the generated homepage-only sitemap available during API outages.
    return next()
  }
}
