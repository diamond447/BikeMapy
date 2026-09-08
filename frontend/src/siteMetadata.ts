const LOCAL_SITE_URL = 'http://localhost:5173'

/** Return the configured public origin, with a useful local-development default. */
export function normalizePublicSiteUrl(value?: string): string {
  const candidate = value?.trim() || LOCAL_SITE_URL
  const parsed = new URL(candidate)
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('VITE_PUBLIC_SITE_URL must use http or https')
  }
  if (parsed.username || parsed.password) {
    throw new Error('VITE_PUBLIC_SITE_URL must not contain credentials')
  }
  return parsed.origin
}

export function routeUrl(siteUrl: string, routeId: string, slug: string): string {
  const url = new URL('/', normalizePublicSiteUrl(siteUrl))
  url.searchParams.set('route', routeId)
  url.searchParams.set('slug', slug)
  return url.href
}

function escapeXml(value: string): string {
  return value.replace(
    /[<>&"']/g,
    (character) =>
      ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[character] ??
      character,
  )
}

export function renderRobots(siteUrl: string): string {
  const origin = normalizePublicSiteUrl(siteUrl)
  return `User-agent: *\nAllow: /\n\nSitemap: ${origin}/sitemap.xml\n`
}

export function renderSitemap(siteUrl: string, urls: string[] = []): string {
  const origin = normalizePublicSiteUrl(siteUrl)
  const locations = [...new Set(['/', ...urls])].map((url) => {
    const absolute = url.startsWith('http') ? url : new URL(url, `${origin}/`).href
    return `<url><loc>${escapeXml(absolute)}</loc></url>`
  })
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ...locations,
    '</urlset>',
    '',
  ].join('\n')
}
