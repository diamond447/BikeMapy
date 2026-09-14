import { describe, expect, it, vi } from 'vitest'

import {
  fetchPublicRoute,
  isRouteId,
  renderRouteDocument,
  routeApiUrl,
} from '../functions/metadata'
import { onRequest as onRouteRequest } from '../functions/index'
import { renderSitemap } from '../functions/sitemap.xml'

const routeId = '123e4567-e89b-12d3-a456-426614174000'

describe('Pages metadata functions', () => {
  it('only accepts UUID route identifiers', () => {
    expect(isRouteId(routeId)).toBe(true)
    expect(isRouteId('not-a-route')).toBe(false)
    expect(isRouteId(null)).toBe(false)
  })

  it('fetches and validates a published route payload', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ id: routeId, slug: 'south-ridge', title: 'South Ridge' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    await expect(fetchPublicRoute(fetcher, 'https://api.example.test', routeId)).resolves.toEqual({
      id: routeId,
      slug: 'south-ridge',
      title: 'South Ridge',
    })
    expect(fetcher).toHaveBeenCalledWith(routeApiUrl('https://api.example.test', routeId), {
      headers: { Accept: 'application/json' },
    })
  })

  it('renders escaped route metadata without retaining generic tags', () => {
    const html = renderRouteDocument(
      '<html><head><title>BikeMapy</title><meta name="description" content="generic" /><meta property="og:title" content="generic" /></head><body></body></html>',
      { id: routeId, slug: 'south-ridge', title: 'South <Ridge>' },
      'https://rides.example.test',
    )
    expect(html).toContain('<title>South &lt;Ridge&gt; · BikeMapy</title>')
    expect(html).toContain('property="og:image" content="https://rides.example.test/og-image.png"')
    expect(html).toContain('rel="canonical" href="https://rides.example.test/?route=')
    expect(html).not.toContain('content="generic"')
  })

  it('serves route metadata through the Pages request handler', async () => {
    const next = vi
      .fn()
      .mockResolvedValue(
        new Response(
          '<html><head><title>BikeMapy</title><meta name="description" content="generic" /></head><body>app</body></html>',
          { headers: { 'content-type': 'text/html' } },
        ),
      )
    vi.stubGlobal(
      'fetch',
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          new Response(JSON.stringify({ id: routeId, slug: 'south-ridge', title: 'South Ridge' })),
        ),
    )
    const response = await onRouteRequest({
      request: new Request(`https://rides.example.test/?route=${routeId}`),
      env: { PUBLIC_API_ORIGIN: 'https://api.example.test' },
      next,
    })
    expect(await response.text()).toContain('<title>South Ridge · BikeMapy</title>')
    expect(next).toHaveBeenCalledOnce()
    vi.unstubAllGlobals()
  })

  it('does not advertise a route that the public API does not return', async () => {
    const fallbackBody = '<html><head><title>BikeMapy</title></head></html>'
    const next = vi.fn().mockResolvedValue(new Response(fallbackBody))
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(new Response('{}', { status: 404 })),
    )
    const response = await onRouteRequest({
      request: new Request(`https://rides.example.test/?route=${routeId}`),
      env: { PUBLIC_API_ORIGIN: 'https://api.example.test' },
      next,
    })
    expect(await response.text()).toBe(fallbackBody)
    vi.unstubAllGlobals()
  })

  it('renders only the homepage and supplied public route URLs', () => {
    const sitemap = renderSitemap('https://rides.example.test', [
      'https://rides.example.test/?route=one&slug=public',
      'https://rides.example.test/?route=one&slug=public',
    ])
    expect(sitemap.match(/<url>/g)).toHaveLength(2)
    expect(sitemap).toContain('https://rides.example.test/?route=one&amp;slug=public')
  })
})
