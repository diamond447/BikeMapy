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

  let response: Response | null = null
  try {
    // Fetch the metadata while Pages serves the shell, so the API round trip
    // overlaps the asset lookup instead of adding to the time to first byte.
    // A metadata failure resolves to null and leaves the shell unchanged.
    const routePromise = fetchPublicRoute(
      fetch,
      apiOrigin(env.PUBLIC_API_ORIGIN ?? env.VITE_API_URL),
      routeId,
    ).catch(() => null)
    response = await next()
    if (!response.ok) return response
    const route = await routePromise
    if (!route) return response
    const html = await response.text()
    const origin = siteOrigin(env.PUBLIC_SITE_URL ?? env.VITE_PUBLIC_SITE_URL, request.url)
    const body = renderRouteDocument(html, route, origin)
    const headers = new Headers(response.headers)
    headers.set('content-type', 'text/html; charset=UTF-8')
    headers.set('cache-control', 'public, max-age=60, s-maxage=60')
    return new Response(body, { status: response.status, headers })
  } catch {
    // A metadata outage must not prevent the application shell from loading.
    // The shell response can only be returned while its body is unread.
    return response && !response.bodyUsed ? response : next()
  }
}
