import {
  apiOrigin,
  fetchPublicRoute,
  isRouteId,
  isRouteRequestPath,
  renderRouteDocument,
  routeIdFromRequest,
  siteOrigin,
} from './metadata'
import type { PagesContext } from './types'

/**
 * Provide crawler-visible metadata for a shared route URL. Other requests are
 * handed back to Pages so the normal static SPA asset and client navigation
 * remain unchanged.
 */
export async function onRequest({ request, env, next }: PagesContext): Promise<Response> {
  if (request.method !== 'GET' || !isRouteRequestPath(new URL(request.url).pathname)) {
    return next()
  }
  const routeId = routeIdFromRequest(request.url)
  if (!isRouteId(routeId)) return next()

  try {
    const route = await fetchPublicRoute(
      fetch,
      apiOrigin(env.PUBLIC_API_ORIGIN ?? env.VITE_API_URL),
      routeId,
    )
    if (!route) return next()
    const response = await next()
    if (!response.ok) return response
    const html = await response.text()
    const origin = siteOrigin(env.PUBLIC_SITE_URL ?? env.VITE_PUBLIC_SITE_URL, request.url)
    const body = renderRouteDocument(html, route, origin)
    const headers = new Headers(response.headers)
    headers.set('content-type', 'text/html; charset=UTF-8')
    headers.set('cache-control', 'public, max-age=60, s-maxage=60')
    return new Response(body, { status: response.status, headers })
  } catch {
    // A metadata outage must not prevent the application shell from loading.
    return next()
  }
}
