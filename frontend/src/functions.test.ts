import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  fetchPublicRoute,
  withAbortDeadline,
  isRouteId,
  renderRouteDocument,
  routeApiUrl,
} from '../functions/metadata'
import { onRequest as onRouteRequest } from '../functions/index'
import { onRequest as onSitemapRequest, renderSitemap } from '../functions/sitemap.xml'

const routeId = '123e4567-e89b-12d3-a456-426614174000'

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

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
    expect(fetcher).toHaveBeenCalledWith(
      routeApiUrl('https://api.example.test', routeId),
      expect.objectContaining({
        headers: { Accept: 'application/json' },
        signal: expect.any(AbortSignal),
      }),
    )
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
  })

  it('aborts an API request after its deadline', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn<typeof fetch>(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('Aborted', 'AbortError')),
          )
        }),
    )
    const pending = withAbortDeadline(
      (signal) => fetcher('https://api.example.test/slow', { signal }),
      25,
    )
    const rejection = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(25)
    await rejection
    expect(fetcher.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
  })

  it('aborts a route whose response body stalls after headers', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          init?.signal?.addEventListener('abort', () =>
            controller.error(new DOMException('Aborted', 'AbortError')),
          )
        },
      })
      return new Response(body, { headers: { 'content-type': 'application/json' } })
    })
    const pending = fetchPublicRoute(fetcher, 'https://api.example.test', routeId, 25)
    const rejection = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(25)
    await rejection
    expect(fetcher.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
  })

  it('paginates the public sitemap and filters malformed route entries', async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      id: `route-${index}`,
      slug: `public-${index}`,
    }))
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const page = new URL(String(input)).searchParams.get('page')
      return new Response(
        JSON.stringify(
          page === '1'
            ? { results: firstPage, next: 'https://api.example.test/routes?page=2' }
            : {
                results: [
                  { id: 'route-final', slug: 'final-route' },
                  { id: 'route-missing-slug' },
                  { slug: 'route-missing-id' },
                  null,
                ],
                next: null,
              },
        ),
      )
    })
    vi.stubGlobal('fetch', fetcher)
    const next = vi.fn()
    const response = await onSitemapRequest({
      request: new Request('https://rides.example.test/sitemap.xml'),
      env: { PUBLIC_API_ORIGIN: 'https://api.example.test' },
      next,
    })
    const body = await response.text()
    expect(response.headers.get('content-type')).toContain('application/xml')
    expect(body.match(/<url>/g)).toHaveLength(102)
    expect(body).toContain('route=route-final')
    expect(body).not.toContain('route-missing-slug')
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(next).not.toHaveBeenCalled()
  })

  it('falls back to the static sitemap when the API fails', async () => {
    const fallbackBody = '<urlset><url><loc>https://rides.example.test/</loc></url></urlset>'
    const next = vi.fn().mockResolvedValue(new Response(fallbackBody))
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(new Response('offline', { status: 503 })),
    )
    const response = await onSitemapRequest({
      request: new Request('https://rides.example.test/sitemap.xml'),
      env: { PUBLIC_API_ORIGIN: 'https://api.example.test' },
      next,
    })
    expect(await response.text()).toBe(fallbackBody)
    expect(next).toHaveBeenCalledOnce()
  })

  it('falls back instead of serving a truncated sitemap over the route limit', async () => {
    const fallbackBody = '<urlset><url><loc>https://rides.example.test/</loc></url></urlset>'
    const next = vi.fn().mockResolvedValue(new Response(fallbackBody))
    const fullPage = Array.from({ length: 100 }, (_, index) => ({
      id: `route-${index}`,
      slug: `public-${index}`,
    }))
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => {
      return new Response(JSON.stringify({ results: fullPage, next: 'next-page' }))
    })
    vi.stubGlobal('fetch', fetcher)
    const response = await onSitemapRequest({
      request: new Request('https://rides.example.test/sitemap.xml'),
      env: { PUBLIC_API_ORIGIN: 'https://api.example.test' },
      next,
    })
    expect(await response.text()).toBe(fallbackBody)
    expect(fetcher).toHaveBeenCalledTimes(45)
    expect(next).toHaveBeenCalledOnce()
  })

  it('passes non-GET sitemap requests through to Pages', async () => {
    const next = vi.fn().mockResolvedValue(new Response('fallback'))
    const fetcher = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetcher)
    const response = await onSitemapRequest({
      request: new Request('https://rides.example.test/sitemap.xml', { method: 'POST' }),
      env: { PUBLIC_API_ORIGIN: 'https://api.example.test' },
      next,
    })
    expect(await response.text()).toBe('fallback')
    expect(next).toHaveBeenCalledOnce()
    expect(fetcher).not.toHaveBeenCalled()
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
