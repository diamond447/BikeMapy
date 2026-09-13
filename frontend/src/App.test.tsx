import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { MockMap } = vi.hoisted(() => {
  class MockMap {
    static last: MockMap | undefined
    sources: Record<string, { setData: ReturnType<typeof vi.fn> }> = {}
    layers = new Set<string>()
    handlers: Record<string, (event?: never) => void> = {}
    features: Array<{ properties: { routeId: string } }> = []
    constructor() {
      MockMap.last = this
    }
    on(event: string, handler: (event?: never) => void) {
      this.handlers[event] = handler
      if (event === 'load') setTimeout(handler, 0)
      return this
    }
    once(event: string, handler: (event?: never) => void) {
      this.on(event, handler)
      if (event === 'idle') setTimeout(handler, 0)
      return this
    }
    addControl() {
      return this
    }
    addSource(id: string) {
      this.sources[id] = { setData: vi.fn() }
      return this
    }
    getSource(id: string) {
      return this.sources[id]
    }
    addLayer(layer: { id: string }) {
      this.layers.add(layer.id)
      return this
    }
    getLayer(id: string) {
      return this.layers.has(id) ? { id } : undefined
    }
    setLayoutProperty() {
      return this
    }
    queryRenderedFeatures() {
      return this.features
    }
    getCenter() {
      return { lng: 16.6, lat: 49.2 }
    }
    getBounds() {
      return { getWest: () => 16, getSouth: () => 49, getEast: () => 17, getNorth: () => 50 }
    }
    getZoom() {
      return 7.5
    }
    jumpTo = vi.fn()
    fitBounds = vi.fn()
    remove() {
      return this
    }
  }
  return { MockMap }
})
vi.mock('maplibre-gl', () => ({
  Map: MockMap,
  AttributionControl: class {},
  NavigationControl: class {},
  setWorkerUrl: vi.fn(),
}))

import type { components } from './api/generated/schema'
import { apiClient } from './api/client'
import { categoriesEnabled, writeUrl } from './discovery/state'
import App from './App'

const route: components['schemas']['Route'] = {
  id: '11111111-1111-4111-8111-111111111111',
  slug: 'south-ridge',
  title: 'South ridge loop',
  categories: [{ slug: 'gravel', name: 'Gravel' }],
  distance_m: '42000.00',
  ascent_m: '630.00',
  descent_m: '620.00',
  loop_status: 'loop',
  source_status: 'verified',
  sources: [
    {
      mapy_url: 'https://mapy.com/s/south-ridge',
      title: 'South ridge on Mapy.com',
      status: 'verified',
      last_checked_at: '2026-02-01T00:00:00Z',
      last_successful_check_at: '2026-02-02T00:00:00Z',
      posts: [
        {
          url: 'https://bikeforum.example/thread/route#post-1',
          thread_title: 'South ridge source discussion',
          thread_url: 'https://bikeforum.example/thread/route',
          author: null,
          posted_at: '2026-01-15T00:00:00Z',
        },
      ],
    },
  ],
  variants: [],
  geometry: null,
  reviewed: false,
  elevation_profile: [
    { distance_m: 0, elevation_m: 220 },
    { distance_m: 42000, elevation_m: 360 },
  ],
  gpx_download_url: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}
const geometry: components['schemas']['SpatialRoute']['geometry'] = {
  type: 'LineString',
  coordinates: [
    [16, 49],
    [16.2, 49.4],
    [16.8, 49.1],
  ],
}
const secondRoute = {
  ...route,
  id: '22222222-2222-4222-8222-222222222222',
  slug: 'north-ridge',
  title: 'North ridge loop',
}
const remoteRoute = {
  ...route,
  id: '33333333-3333-4333-8333-333333333333',
  slug: 'remote-route',
  title: 'Remote route outside page',
}
const thirdRoute = {
  ...route,
  id: '44444444-4444-4444-8444-444444444444',
  slug: 'third-ridge',
  title: 'Third ridge loop',
}

function mockApi(success = false) {
  return vi.spyOn(apiClient, 'GET').mockImplementation(((path: string) => {
    if (!success) return Promise.resolve({ data: undefined, error: { detail: 'offline' } }) as never
    if (path.includes('/geometry/')) {
      const selected = path.includes(secondRoute.id)
        ? secondRoute
        : path.includes(remoteRoute.id)
          ? remoteRoute
          : route
      return Promise.resolve({
        data: { id: selected.id, slug: selected.slug, title: selected.title, geometry },
        error: undefined,
      }) as never
    }
    if (path.includes('/{route_id}/')) {
      return Promise.resolve({ data: remoteRoute, error: undefined }) as never
    }
    if (path.includes('/viewport/'))
      return Promise.resolve({
        data: { mode: 'heatmap', zoom: 7, data_zoom: 7, cells: [], routes: [], truncated: false },
        error: undefined,
      }) as never
    return Promise.resolve({
      data: { count: 2, next: null, previous: null, results: [route, secondRoute] },
      error: undefined,
    }) as never
  }) as never)
}

function mockRoutePages(handler: (query: Record<string, string>) => unknown) {
  return vi.spyOn(apiClient, 'GET').mockImplementation(((path: string, options?: unknown) => {
    if (path === '/api/v1/routes/') {
      const query =
        (options as { params?: { query?: Record<string, string> } })?.params?.query ?? {}
      return Promise.resolve({ data: handler(query), error: undefined }) as never
    }
    if (path.includes('/geometry/'))
      return Promise.resolve({
        data: { id: route.id, slug: route.slug, title: route.title, geometry },
        error: undefined,
      }) as never
    if (path.includes('/viewport/'))
      return Promise.resolve({
        data: { mode: 'heatmap', zoom: 7, data_zoom: 7, cells: [], routes: [], truncated: false },
        error: undefined,
      }) as never
    return Promise.resolve({ data: remoteRoute, error: undefined }) as never
  }) as never)
}

describe('BikeMapy route discovery', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState({}, '', '/')
    mockApi()
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('introduces the catalogue and exposes the map landmarks', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: /find the ride/i })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /route map/i })).toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: /search routes/i })).toBeInTheDocument()
  })

  it('exposes legal documents, independence notice, and map attribution in the footer', () => {
    render(<App />)
    expect(screen.getByRole('contentinfo', { name: /legal and attribution/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Terms' })).toHaveAttribute(
      'href',
      'https://github.com/diamond447/BikeMapy/blob/main/docs/terms.md',
    )
    expect(screen.getByRole('link', { name: 'Privacy' })).toHaveAttribute(
      'href',
      'https://github.com/diamond447/BikeMapy/blob/main/docs/privacy.md',
    )
    expect(screen.getByRole('link', { name: 'Removal Policy' })).toHaveAttribute(
      'href',
      'https://github.com/diamond447/BikeMapy/blob/main/docs/removal-policy.md',
    )
    expect(
      screen.getByText(/independent project; no affiliation with mapy\.com/i),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'OpenFreeMap' })).toHaveAttribute(
      'href',
      'https://openfreemap.org/',
    )
    expect(screen.getByRole('link', { name: 'OpenStreetMap contributors' })).toHaveAttribute(
      'href',
      'https://www.openstreetmap.org/copyright',
    )
  })

  it('renders a route, selects it, and requests the full geometry', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    const card = await screen.findByRole('button', { name: /south ridge loop/i })
    await user.click(card)
    expect(await screen.findByRole('heading', { name: /south ridge loop/i })).toBeInTheDocument()
    expect(window.location.search).toContain(`route=${route.id}`)
    expect(apiClient.GET).toHaveBeenCalledWith('/api/v1/routes/{route_id}/geometry/', {
      params: { path: { route_id: route.id } },
    })
  })

  it('appends API pages through keyboard activation and keeps the selected route', async () => {
    vi.restoreAllMocks()
    const routeCalls: Array<Record<string, string>> = []
    const api = vi.spyOn(apiClient, 'GET').mockImplementation(((
      path: string,
      options?: unknown,
    ) => {
      if (path === '/api/v1/routes/') {
        const query =
          (options as { params?: { query?: Record<string, string> } })?.params?.query ?? {}
        routeCalls.push(query)
        return Promise.resolve({
          data:
            query.page === '2'
              ? { count: 3, next: null, previous: null, results: [secondRoute, thirdRoute] }
              : {
                  count: 3,
                  next: '/api/v1/routes/?page=2&page_size=100',
                  previous: null,
                  results: [route],
                },
          error: undefined,
        }) as never
      }
      if (path.includes('/geometry/'))
        return Promise.resolve({
          data: { id: route.id, slug: route.slug, title: route.title, geometry },
          error: undefined,
        }) as never
      if (path.includes('/viewport/'))
        return Promise.resolve({
          data: { mode: 'heatmap', zoom: 7, data_zoom: 7, cells: [], routes: [], truncated: false },
          error: undefined,
        }) as never
      return Promise.resolve({ data: remoteRoute, error: undefined }) as never
    }) as never)
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    expect(await screen.findByRole('heading', { name: /south ridge loop/i })).toBeInTheDocument()
    const loadMore = await screen.findByRole('button', { name: /load more routes/i })
    loadMore.focus()
    await user.keyboard('{Enter}')

    expect(await screen.findByRole('button', { name: /north ridge loop/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /third ridge loop/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /south ridge loop/i })).toBeInTheDocument()
    expect(window.location.search).toContain(`route=${route.id}`)
    expect(screen.getByText('3 of 3 routes loaded')).toHaveAttribute('role', 'status')
    expect(screen.getByText('All matching routes are loaded.')).toBeInTheDocument()
    expect(routeCalls).toEqual(
      expect.arrayContaining([expect.objectContaining({ page: '2', page_size: '100' })]),
    )
    expect(api).toHaveBeenCalledWith('/api/v1/routes/', {
      params: { query: { page: '2', page_size: '100' } },
      signal: expect.any(AbortSignal),
    })
  })

  it('shows completion when a later page is empty', async () => {
    vi.restoreAllMocks()
    const api = vi.spyOn(apiClient, 'GET').mockImplementation(((
      path: string,
      options?: unknown,
    ) => {
      if (path === '/api/v1/routes/') {
        const query =
          (options as { params?: { query?: Record<string, string> } })?.params?.query ?? {}
        return Promise.resolve({
          data:
            query.page === '2'
              ? { count: 1, next: null, previous: null, results: [] }
              : {
                  count: 1,
                  next: '/api/v1/routes/?page=2&page_size=100',
                  previous: null,
                  results: [route],
                },
          error: undefined,
        }) as never
      }
      if (path.includes('/viewport/'))
        return Promise.resolve({
          data: { mode: 'heatmap', zoom: 7, data_zoom: 7, cells: [], routes: [], truncated: false },
          error: undefined,
        }) as never
      return Promise.resolve({ data: remoteRoute, error: undefined }) as never
    }) as never)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /load more routes/i }))
    expect(await screen.findByText('All matching routes are loaded.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /load more routes/i })).not.toBeInTheDocument()
    expect(api).toHaveBeenCalledWith('/api/v1/routes/', {
      params: { query: { page: '2', page_size: '100' } },
      signal: expect.any(AbortSignal),
    })
  })

  it('preserves loaded routes and retries a failed later page', async () => {
    vi.restoreAllMocks()
    let laterAttempt = 0
    vi.spyOn(apiClient, 'GET').mockImplementation(((path: string, options?: unknown) => {
      if (path === '/api/v1/routes/') {
        const query =
          (options as { params?: { query?: Record<string, string> } })?.params?.query ?? {}
        if (query.page === '2') {
          laterAttempt += 1
          return laterAttempt === 1
            ? Promise.reject(new Error('offline'))
            : (Promise.resolve({
                data: { count: 2, next: null, previous: null, results: [secondRoute] },
                error: undefined,
              }) as never)
        }
        return Promise.resolve({
          data: {
            count: 2,
            next: '/api/v1/routes/?page=2&page_size=100',
            previous: null,
            results: [route],
          },
          error: undefined,
        }) as never
      }
      if (path.includes('/viewport/'))
        return Promise.resolve({
          data: { mode: 'heatmap', zoom: 7, data_zoom: 7, cells: [], routes: [], truncated: false },
          error: undefined,
        }) as never
      return Promise.resolve({ data: remoteRoute, error: undefined }) as never
    }) as never)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /load more routes/i }))
    expect(await screen.findByText('More routes could not be loaded.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /south ridge loop/i })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /retry loading routes/i }))
    expect(await screen.findByRole('button', { name: /north ridge loop/i })).toBeInTheDocument()
    expect(laterAttempt).toBe(2)
  })

  it('ignores a stale later-page response after a filter change', async () => {
    vi.restoreAllMocks()
    let resolveLater: ((value: unknown) => void) | undefined
    vi.spyOn(apiClient, 'GET').mockImplementation(((path: string, options?: unknown) => {
      if (path === '/api/v1/routes/') {
        const query =
          (options as { params?: { query?: Record<string, string> } })?.params?.query ?? {}
        if (query.page === '2')
          return new Promise((resolve) => {
            resolveLater = resolve
          }) as never
        if (query.search === 'new')
          return Promise.resolve({
            data: { count: 1, next: null, previous: null, results: [secondRoute] },
            error: undefined,
          }) as never
        return Promise.resolve({
          data: {
            count: 2,
            next: '/api/v1/routes/?page=2&page_size=100',
            previous: null,
            results: [route],
          },
          error: undefined,
        }) as never
      }
      if (path.includes('/viewport/'))
        return Promise.resolve({
          data: { mode: 'heatmap', zoom: 7, data_zoom: 7, cells: [], routes: [], truncated: false },
          error: undefined,
        }) as never
      return Promise.resolve({ data: remoteRoute, error: undefined }) as never
    }) as never)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /load more routes/i }))
    fireEvent.change(screen.getByRole('searchbox', { name: /search routes/i }), {
      target: { value: 'new' },
    })
    expect(await screen.findByRole('button', { name: /north ridge loop/i })).toBeInTheDocument()
    resolveLater?.({
      data: { count: 2, next: null, previous: null, results: [remoteRoute] },
      error: undefined,
    })
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /remote route outside page/i }),
      ).not.toBeInTheDocument(),
    )
  })

  it('deduplicates a page and stops when its next link repeats', async () => {
    const pageTwo = '/api/v1/routes/?page=2&page_size=100'
    vi.restoreAllMocks()
    mockRoutePages((query) =>
      query.page === '2'
        ? { count: 3, next: pageTwo, previous: null, results: [secondRoute, secondRoute] }
        : { count: 3, next: pageTwo, previous: null, results: [route] },
    )
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /load more routes/i }))
    expect(screen.getAllByRole('button', { name: /north ridge loop/i })).toHaveLength(1)
    expect(await screen.findByText('The route results are incomplete.')).toBeInTheDocument()
    expect(screen.queryByText('All matching routes are loaded.')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /load more routes/i })).not.toBeInTheDocument()
  })

  it('stops a later page when its next link forms a cycle', async () => {
    const pageTwo = '/api/v1/routes/?page=2&page_size=100'
    const pageThree = '/api/v1/routes/?page=3&page_size=100'
    vi.restoreAllMocks()
    mockRoutePages((query) => {
      if (query.page === '2')
        return { count: 3, next: pageThree, previous: null, results: [secondRoute] }
      if (query.page === '3') return { count: 3, next: pageTwo, previous: null, results: [] }
      return { count: 3, next: pageTwo, previous: null, results: [route] }
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /load more routes/i }))
    await user.click(await screen.findByRole('button', { name: /load more routes/i }))
    expect(await screen.findByText('The route results are incomplete.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /north ridge loop/i })).toBeInTheDocument()
    expect(screen.queryByText('All matching routes are loaded.')).not.toBeInTheDocument()
  })

  it('rejects a malformed next link without a pagination cursor', async () => {
    vi.restoreAllMocks()
    mockRoutePages(() => ({
      count: 2,
      next: '/api/v1/routes/?page_size=100',
      previous: null,
      results: [route],
    }))
    render(<App />)
    expect(await screen.findByText('The route results are incomplete.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /load more routes/i })).not.toBeInTheDocument()
    expect(screen.queryByText('All matching routes are loaded.')).not.toBeInTheDocument()
  })

  it('does not claim completion when the API count exceeds the loaded routes', async () => {
    vi.restoreAllMocks()
    mockRoutePages(() => ({ count: 2, next: null, previous: null, results: [route] }))
    render(<App />)
    expect(await screen.findByText('The route results are incomplete.')).toBeInTheDocument()
    expect(screen.getByText('1 of 2 routes loaded')).toBeInTheDocument()
    expect(screen.queryByText('All matching routes are loaded.')).not.toBeInTheDocument()
  })

  it('normalizes a lower initial count without claiming completion', async () => {
    vi.restoreAllMocks()
    mockRoutePages(() => ({
      count: 1,
      next: null,
      previous: null,
      results: [route, secondRoute],
    }))
    render(<App />)
    expect(await screen.findByText('The route results are incomplete.')).toBeInTheDocument()
    expect(screen.getByText('2 of 2 routes loaded')).toBeInTheDocument()
    expect(screen.queryByText('All matching routes are loaded.')).not.toBeInTheDocument()
  })

  it('normalizes a lower later-page count without claiming completion', async () => {
    const pageTwo = '/api/v1/routes/?page=2&page_size=100'
    vi.restoreAllMocks()
    mockRoutePages((query) =>
      query.page === '2'
        ? { count: 1, next: null, previous: null, results: [secondRoute] }
        : { count: 3, next: pageTwo, previous: null, results: [route] },
    )
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /load more routes/i }))
    expect(await screen.findByText('The route results are incomplete.')).toBeInTheDocument()
    expect(screen.getByText('2 of 2 routes loaded')).toBeInTheDocument()
    expect(screen.queryByText('All matching routes are loaded.')).not.toBeInTheDocument()
  })

  it('tracks detail and source interactions, resets on close, and keeps GPX disabled', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const beacon = vi.fn().mockReturnValue(true)
    Object.defineProperty(navigator, 'sendBeacon', { configurable: true, value: beacon })
    const user = userEvent.setup()
    render(<App />)

    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await screen.findByRole('heading', { name: /south ridge loop/i })
    await waitFor(() => expect(beacon).toHaveBeenCalledTimes(1))
    expect(beacon.mock.calls[0][1].toString()).toBe('event=route_detail_view')
    fireEvent.click(screen.getByRole('link', { name: /open mapy\.com route/i }))
    expect(beacon.mock.calls[1][1].toString()).toBe('event=original_source_click')
    expect(screen.getByRole('button', { name: /download gpx/i })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: /close route details/i }))
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await waitFor(() => expect(beacon).toHaveBeenCalledTimes(3))
    expect(beacon.mock.calls[2][1].toString()).toBe('event=route_detail_view')
  })

  it('sends text and numeric filters and can clear them', async () => {
    vi.restoreAllMocks()
    const api = mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    const search = screen.getByRole('searchbox', { name: /search routes/i })
    await user.type(search, 'ridge')
    const author = screen.getByLabelText('Author')
    await user.type(author, 'Jana')
    await waitFor(() => expect(window.location.search).toContain('q=ridge'))
    await user.click(screen.getByRole('button', { name: /clear filters/i }))
    expect(search).toHaveValue('')
    expect(author).toHaveValue('')
    expect(api).toHaveBeenCalled()
  })

  it('debounces rapid catalogue filters and aborts superseded list and viewport requests', async () => {
    vi.restoreAllMocks()
    const calls: Array<{
      path: string
      query: Record<string, unknown>
      signal: AbortSignal | undefined
    }> = []
    let initialListResolve: ((value: unknown) => void) | undefined
    let initialViewportResolve: ((value: unknown) => void) | undefined
    const api = vi.spyOn(apiClient, 'GET').mockImplementation(((
      path: string,
      options?: unknown,
    ) => {
      const request = options as {
        params?: { query?: Record<string, unknown> }
        signal?: AbortSignal
      }
      const query = request.params?.query ?? {}
      calls.push({ path, query, signal: request.signal })
      if (path === '/api/v1/routes/' && calls.filter((call) => call.path === path).length === 1)
        return new Promise((resolve) => {
          initialListResolve = resolve
        }) as never
      if (
        path.includes('/viewport/') &&
        calls.filter((call) => call.path.includes('/viewport/')).length === 1
      )
        return new Promise((resolve) => {
          initialViewportResolve = resolve
        }) as never
      if (path.includes('/viewport/'))
        return Promise.resolve({
          data: { mode: 'heatmap', zoom: 7, data_zoom: 7, cells: [], routes: [], truncated: false },
          error: undefined,
        }) as never
      return Promise.resolve({
        data: { count: 1, next: null, previous: null, results: [secondRoute] },
        error: undefined,
      }) as never
    }) as never)

    render(<App />)
    await waitFor(() =>
      expect(calls.filter((call) => call.path === '/api/v1/routes/')).toHaveLength(1),
    )
    await waitFor(() =>
      expect(calls.filter((call) => call.path.includes('/viewport/'))).toHaveLength(1),
    )

    fireEvent.change(screen.getByRole('searchbox', { name: /search routes/i }), {
      target: { value: 'r' },
    })
    fireEvent.change(screen.getByRole('searchbox', { name: /search routes/i }), {
      target: { value: 'ridge' },
    })
    fireEvent.change(screen.getByLabelText('Distance from (m)'), {
      target: { value: '1' },
    })
    fireEvent.change(screen.getByLabelText('Distance from (m)'), {
      target: { value: '1000' },
    })

    await waitFor(() => {
      expect(
        calls.filter(
          (call) => call.query.search === 'ridge' && call.query.min_distance_m === '1000',
        ),
      ).toHaveLength(2)
    })
    expect(calls.filter((call) => call.path === '/api/v1/routes/')).toHaveLength(2)
    expect(calls.filter((call) => call.path.includes('/viewport/'))).toHaveLength(2)
    expect(calls[0].signal?.aborted).toBe(true)
    expect(calls[1].signal?.aborted).toBe(true)

    initialListResolve?.({
      data: { count: 1, next: null, previous: null, results: [route] },
      error: undefined,
    })
    initialViewportResolve?.({
      data: {
        mode: 'heatmap',
        zoom: 7,
        data_zoom: 7,
        cells: [],
        routes: [],
        truncated: false,
      },
      error: undefined,
    })
    expect(await screen.findByRole('button', { name: /north ridge loop/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /south ridge loop/i })).not.toBeInTheDocument()
    expect(api).toHaveBeenCalled()
  })

  it('persists non-geographic preferences while keeping the viewport in the URL', async () => {
    render(<App />)

    await waitFor(() => {
      const saved = JSON.parse(localStorage.getItem('bikemapy:discovery-state') ?? '{}') as Record<
        string,
        unknown
      >
      expect(saved).toEqual(
        expect.objectContaining({
          filters: expect.any(Object),
          routeId: null,
          viewportOnly: false,
        }),
      )
      expect(saved).not.toHaveProperty('view')
    })
    expect(window.location.search).toContain('lng=16.6000')
    expect(window.location.search).toContain('lat=49.2000')
  })

  it('resolves a selected route outside the current result page', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    window.history.replaceState({}, '', `/?route=${remoteRoute.id}`)
    render(<App />)
    expect(
      await screen.findByRole('heading', { name: /remote route outside page/i }),
    ).toBeInTheDocument()
  })

  it('updates the map camera and viewport mode from browser navigation', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    render(<App />)
    await waitFor(() => expect(MockMap.last?.jumpTo).toBeDefined())
    window.history.pushState({}, '', '/?lng=18&lat=50&z=10')
    window.dispatchEvent(new PopStateEvent('popstate'))
    expect(MockMap.last?.jumpTo).toHaveBeenCalledWith({ center: [18, 50], zoom: 10 })
    const viewportToggle = await screen.findByRole('checkbox', { name: /current viewport/i })
    await userEvent.click(viewportToggle)
    expect(window.location.search).toContain('inview=1')
  })

  it('sends catalogue filters to the map and bounds to current-viewport lists', async () => {
    vi.restoreAllMocks()
    const api = mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    await user.type(await screen.findByRole('searchbox', { name: /search routes/i }), 'ridge')
    await waitFor(() => {
      expect(
        api.mock.calls.some((call) => {
          const [path, options] = call as [string, { params?: { query?: Record<string, unknown> } }]
          return path.includes('/viewport/') && options.params?.query?.search === 'ridge'
        }),
      ).toBe(true)
    })
    await user.click(screen.getByRole('checkbox', { name: /current viewport/i }))
    await waitFor(() => {
      expect(
        api.mock.calls.some((call) => {
          const [path, options] = call as [string, { params?: { query?: Record<string, unknown> } }]
          return path === '/api/v1/routes/' && options.params?.query?.west === '16'
        }),
      ).toBe(true)
    })
  })

  it('retries a failed catalogue request and recovers', async () => {
    vi.restoreAllMocks()
    let online = false
    const api = vi.spyOn(apiClient, 'GET').mockImplementation(((path: string) => {
      if (!online)
        return Promise.resolve({ data: undefined, error: { detail: 'offline' } }) as never
      if (path.includes('/viewport/'))
        return Promise.resolve({
          data: { mode: 'heatmap', zoom: 7, cells: [], routes: [], truncated: false },
          error: undefined,
        }) as never
      return Promise.resolve({
        data: { count: 1, next: null, previous: null, results: [route] },
        error: undefined,
      }) as never
    }) as never)
    const user = userEvent.setup()
    render(<App />)
    expect(await screen.findByText(/lost the archive connection/i)).toBeInTheDocument()
    const before = api.mock.calls.length
    online = true
    await user.click(screen.getByRole('button', { name: /^retry$/i }))
    expect(await screen.findByRole('button', { name: /south ridge loop/i })).toBeInTheDocument()
    expect(api.mock.calls.length).toBeGreaterThan(before)
  })

  it('shows independent route-detail errors with targeted retries', async () => {
    vi.restoreAllMocks()
    let metadataOnline = false
    let geometryOnline = false
    const api = vi.spyOn(apiClient, 'GET').mockImplementation(((path: string) => {
      if (path.includes('/geometry/')) {
        return (
          geometryOnline
            ? Promise.resolve({
                data: {
                  id: remoteRoute.id,
                  slug: remoteRoute.slug,
                  title: remoteRoute.title,
                  geometry,
                },
                error: undefined,
              })
            : Promise.resolve({ data: undefined, error: { detail: 'geometry offline' } })
        ) as never
      }
      if (path.includes('/{route_id}/')) {
        return (
          metadataOnline
            ? Promise.resolve({ data: remoteRoute, error: undefined })
            : Promise.resolve({ data: undefined, error: { detail: 'metadata offline' } })
        ) as never
      }
      if (path.includes('/viewport/'))
        return Promise.resolve({
          data: { mode: 'heatmap', zoom: 7, cells: [], routes: [], truncated: false },
          error: undefined,
        }) as never
      return Promise.resolve({
        data: { count: 0, next: null, previous: null, results: [] },
        error: undefined,
      }) as never
    }) as never)
    const user = userEvent.setup()
    window.history.replaceState({}, '', `/?route=${remoteRoute.id}`)
    render(<App />)
    expect(await screen.findByText(/route details are unavailable/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry route details/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry geometry/i })).toBeInTheDocument()

    metadataOnline = true
    await user.click(screen.getByRole('button', { name: /retry route details/i }))
    expect(
      await screen.findByRole('heading', { name: /remote route outside page/i }),
    ).toBeInTheDocument()

    geometryOnline = true
    await user.click(screen.getByRole('button', { name: /retry geometry/i }))
    await waitFor(() =>
      expect(api.mock.calls.some((call) => (call as [string])[0].includes('/geometry/'))).toBe(
        true,
      ),
    )
  })

  it('frames selected routes against open and collapsed mobile sheets', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const originalWidth = window.innerWidth
    const originalHeight = window.innerHeight
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await waitFor(() => expect(MockMap.last?.fitBounds).toHaveBeenCalled())
    const sheetToggle = screen.getByRole('button', { name: /hide routes/i })
    expect(sheetToggle).toBeDisabled()
    const collapsedCall = MockMap.last?.fitBounds.mock.calls.at(-1)
    expect(collapsedCall?.[1].padding.bottom).toBe(42 + 150 + 24)
    expect(80 + Number(collapsedCall?.[1].padding.bottom)).toBeLessThan(844)

    await user.click(screen.getByRole('button', { name: /close route details/i }))
    expect(screen.getByRole('button', { name: /show routes/i })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /show routes/i }))
    expect(screen.getByRole('button', { name: /expand routes/i })).toBeInTheDocument()
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 667 })
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await waitFor(() => {
      const commonPhoneCall = MockMap.last?.fitBounds.mock.calls.at(-1)
      const bottom = Number(commonPhoneCall?.[1].padding.bottom)
      expect(bottom).toBe(42 + 150 + 24)
      expect(80 + bottom).toBeLessThan(667)
    })
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalHeight })
  })

  it('persists state in a compact shareable URL', () => {
    writeUrl(
      {
        search: 'river walk',
        author: '',
        category: 'gravel',
        min_distance_m: '',
        max_distance_m: '',
        min_ascent_m: '200',
        max_ascent_m: '',
      },
      { longitude: 16.6, latitude: 49.2, zoom: 8 },
      route.id,
      'replace',
    )
    expect(window.location.search).toContain('q=river+walk')
    expect(window.location.search).toContain('category=gravel')
    expect(window.location.search).toContain('route=')
  })

  it('handles a cleared selection and a recoverable API error', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await user.click(await screen.findByRole('button', { name: /close route details/i }))
    expect(screen.queryByRole('heading', { name: /south ridge loop/i })).not.toBeInTheDocument()
    vi.restoreAllMocks()
    mockApi()
    render(<App />)
    expect(await screen.findByText(/lost the archive connection/i)).toBeInTheDocument()
  })

  it('synchronizes map click overlap and panel state', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    const card = await screen.findByRole('button', { name: /south ridge loop/i })
    await user.click(card)
    await waitFor(() => expect(Object.keys(MockMap.last?.sources ?? {})).toHaveLength(3))
    const map = MockMap.last!
    const secondId = secondRoute.id
    map.features = [{ properties: { routeId: route.id } }, { properties: { routeId: secondId } }]
    map.handlers.moveend?.()
    map.handlers.click?.({ point: { x: 1, y: 1 } } as never)
    expect(await screen.findByText('1 / 2')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /next overlapping route/i }))
    expect(await screen.findByText('2 / 2')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /hide routes/i }))
    expect(screen.getByRole('button', { name: /show routes/i })).toBeInTheDocument()
  })

  it('keeps keyboard hover and selection indicators synchronized with the map', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    const card = await screen.findByRole('button', { name: /south ridge loop/i })
    await user.hover(card)
    expect(card).toHaveClass('is-hovered')
    expect(document.querySelector('.map-canvas')).toHaveAttribute('data-hovered-route', route.id)
    await user.unhover(card)
    expect(card).not.toHaveClass('is-hovered')
    await user.click(card)
    expect(card).toHaveClass('is-selected')
    expect(card).toHaveAttribute('aria-current', 'true')
    expect(card.querySelector('.route-card-marker')).toHaveTextContent('◆')
  })

  it('offers collapsed, half, and full mobile sheet positions', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const originalWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
    const user = userEvent.setup()
    render(<App />)
    const sheetToggle = screen.getByRole('button', { name: /show routes/i })
    expect(screen.getByTestId('sheet-position')).toHaveAttribute('data-position', 'collapsed')
    await user.click(sheetToggle)
    expect(screen.getByTestId('sheet-position')).toHaveAttribute('data-position', 'half')
    expect(screen.getByRole('button', { name: /expand routes/i })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /expand routes/i }))
    expect(screen.getByTestId('sheet-position')).toHaveAttribute('data-position', 'full')
    await user.click(screen.getByRole('button', { name: /hide routes/i }))
    expect(screen.getByTestId('sheet-position')).toHaveAttribute('data-position', 'collapsed')
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
  })

  it('covers numeric filters, history navigation, and retry', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    await user.type(await screen.findByLabelText('Distance from (m)'), '1000')
    await user.type(screen.getByLabelText('Distance to (m)'), '80000')
    await user.type(screen.getByLabelText('Climb from (m)'), '100')
    await user.type(screen.getByLabelText('Climb to (m)'), '1000')
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument()
    window.dispatchEvent(new PopStateEvent('popstate'))
    vi.restoreAllMocks()
    mockApi()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /^retry$/i }))
  })

  it('keeps category UI disabled by default and enables it from the build flag', async () => {
    expect(categoriesEnabled()).toBe(false)
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument()
    vi.stubEnv('VITE_ENABLE_CATEGORIES', 'true')
    expect(categoriesEnabled()).toBe(true)
    vi.restoreAllMocks()
    mockApi(true)
    render(<App />)
    expect(await screen.findByLabelText('Category')).toBeInTheDocument()
    expect(screen.getAllByText('Gravel').length).toBeGreaterThan(0)
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument()
    vi.unstubAllEnvs()
  })

  it('does not send a parsed category to APIs while category support is disabled', async () => {
    window.history.replaceState({}, '', '/?category=gravel')
    vi.restoreAllMocks()
    const api = mockApi(true)
    render(<App />)
    await waitFor(() => expect(api).toHaveBeenCalled())
    expect(api.mock.calls.every((call) => !JSON.stringify(call).includes('gravel'))).toBe(true)
    window.history.pushState({}, '', '/?category=gravel&author=Jana')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect(api.mock.calls.length).toBeGreaterThan(2))
    expect(api.mock.calls.every((call) => !JSON.stringify(call).includes('gravel'))).toBe(true)
  })

  it('keeps route detail content in the accessible atlas order', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    const detail = await screen.findByRole('region', { name: /selected route/i }).catch(() => null)
    const section = detail ?? document.querySelector('.route-detail')
    expect(section).toBeTruthy()
    const text = section?.textContent ?? ''
    expect(text.indexOf('South ridge loop')).toBeLessThan(text.indexOf('Distance'))
    expect(text.indexOf('Distance')).toBeLessThan(text.indexOf('Elevation profile'))
    expect(text.indexOf('Elevation profile')).toBeLessThan(text.indexOf('BikeForum sources'))
    expect(text.indexOf('BikeForum sources')).toBeLessThan(text.indexOf('Share route'))
    expect(text.indexOf('Share route')).toBeLessThan(text.indexOf('Report a problem'))
  })

  it('switches and persists Czech copy while updating route metadata', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await screen.findByRole('heading', { name: /south ridge loop/i })
    expect(document.title).toContain('South ridge loop')
    await user.click(screen.getByRole('button', { name: /change language/i }))
    expect(document.documentElement.lang).toBe('cs')
    expect(screen.getByRole('heading', { name: /najděte trasu/i })).toBeInTheDocument()
    expect(localStorage.getItem('bikemapy:language')).toBe('cs')
  })

  it('offers a permanent link and an accessible report dialog', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await user.click(await screen.findByRole('button', { name: /copy permanent link/i }))
    expect(await screen.findByText(/link copied|copy unavailable/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /report a problem/i }))
    expect(
      screen.getByRole('dialog', { name: /report a problem with this route/i }),
    ).toBeInTheDocument()
    expect(screen.getByRole('group', { name: /complete the security check/i })).toBeInTheDocument()
    expect(document.activeElement).toBe(
      screen.getByRole('textbox', { name: /what should we check/i }),
    )
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /report a problem/i }))
    await user.type(
      screen.getByRole('textbox', { name: /what should we check/i }),
      'Wrong geometry',
    )
    await user.click(screen.getByRole('button', { name: /report unavailable/i }))
    expect(screen.getByText(/no report was submitted/i)).toBeInTheDocument()
  })

  it('submits a localized report and shows the review-queue outcome', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ status: 'received' }) })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await user.click(await screen.findByRole('button', { name: /report a problem/i }))
    await user.selectOptions(screen.getByRole('combobox', { name: /reason/i }), 'rights_holder')
    await user.type(
      screen.getByRole('textbox', { name: /what should we check/i }),
      'Please review rights.',
    )
    fireEvent.change(document.querySelector('input[name="turnstile_token"]')!, {
      target: { value: 'verified-token' },
    })
    await user.click(screen.getByRole('button', { name: /send report/i }))
    expect(await screen.findByText(/entered the review queue/i)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/reports/'),
      expect.objectContaining({ method: 'POST' }),
    )
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string).website).toBe('')
  })

  it('sends a filled honeypot and surfaces the rejected outcome', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: 'Unable to submit this report.' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await user.click(await screen.findByRole('button', { name: /report a problem/i }))
    await user.type(
      screen.getByRole('textbox', { name: /what should we check/i }),
      'Automated text.',
    )
    fireEvent.change(document.querySelector('input[name="turnstile_token"]')!, {
      target: { value: 'verified-token' },
    })
    fireEvent.change(document.querySelector('input[name="website"]')!, {
      target: { value: 'https://bot.example' },
    })
    await user.click(screen.getByRole('button', { name: /send report/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/security verification failed/i)
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string).website).toBe(
      'https://bot.example',
    )
  })

  it('resets consumed Turnstile tokens and allows a duplicate report retry', async () => {
    vi.restoreAllMocks()
    vi.stubEnv('VITE_TURNSTILE_SITE_KEY', 'test-site-key')
    mockApi(true)
    type MockResponse = {
      ok: boolean
      status: number
      json: () => Promise<Record<string, string>>
    }
    let releaseFirstResponse!: (response: MockResponse) => void
    const firstResponse = new Promise<MockResponse>((resolve) => {
      releaseFirstResponse = resolve
    })
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(firstResponse)
      .mockResolvedValueOnce({
        ok: false,
        status: 429,
        json: async () => ({ detail: 'Report rate limit reached.' }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ status: 'received' }) })
    vi.stubGlobal('fetch', fetchMock)
    const turnstileReset = vi.fn()
    vi.stubGlobal('turnstile', {
      render: vi.fn(() => 'test-widget'),
      reset: turnstileReset,
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await user.click(await screen.findByRole('button', { name: /report a problem/i }))
    await waitFor(() => expect(turnstileReset).not.toHaveBeenCalled())
    const token = document.querySelector<HTMLInputElement>('input[name="turnstile_token"]')!
    await user.type(
      screen.getByRole('textbox', { name: /what should we check/i }),
      'The route needs a review.',
    )
    fireEvent.change(token, { target: { value: 'consumed-token' } })
    const submit = screen.getByRole('button', { name: /send report/i })
    const submission = user.click(submit)
    await waitFor(() => expect(submit).toBeDisabled())
    const outsideFocusTarget = document.createElement('button')
    document.body.append(outsideFocusTarget)
    outsideFocusTarget.focus()
    expect(document.activeElement).toBe(outsideFocusTarget)
    releaseFirstResponse({
      ok: false,
      status: 409,
      json: async () => ({ detail: 'A matching report was already submitted.' }),
    })
    await submission
    outsideFocusTarget.remove()

    expect(await screen.findByRole('alert')).toHaveTextContent(/already submitted/i)
    expect(turnstileReset).toHaveBeenCalledWith('test-widget')
    expect(token).toHaveValue('')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /send report/i }))

    fireEvent.change(token, { target: { value: 'rate-limited-token' } })
    await user.click(screen.getByRole('button', { name: /send report/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/limit was reached/i)
    expect(turnstileReset).toHaveBeenCalledTimes(2)
    expect(token).toHaveValue('')

    fireEvent.change(token, { target: { value: 'fresh-token' } })
    await user.click(screen.getByRole('button', { name: /send report/i }))
    expect(await screen.findByText(/entered the review queue/i)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(JSON.parse(fetchMock.mock.calls[2][1].body as string).message).toBe(
      'The route needs a review.',
    )
    expect(JSON.parse(fetchMock.mock.calls[2][1].body as string).turnstile_token).toBe(
      'fresh-token',
    )
  })

  it('resets Turnstile after verification and network failures while preserving fields', async () => {
    vi.restoreAllMocks()
    vi.stubEnv('VITE_TURNSTILE_SITE_KEY', 'test-site-key')
    mockApi(true)
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 400,
        json: async () => ({ detail: 'Security verification failed.' }),
      })
      .mockRejectedValueOnce(new Error('offline'))
    vi.stubGlobal('fetch', fetchMock)
    const turnstileReset = vi.fn()
    vi.stubGlobal('turnstile', {
      render: vi.fn(() => 'test-widget'),
      reset: turnstileReset,
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await user.click(await screen.findByRole('button', { name: /report a problem/i }))
    const message = screen.getByRole('textbox', { name: /what should we check/i })
    const token = document.querySelector<HTMLInputElement>('input[name="turnstile_token"]')!
    await user.type(message, 'Please check this route.')
    fireEvent.change(token, { target: { value: 'expired-token' } })
    await user.click(screen.getByRole('button', { name: /send report/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/security verification failed/i)
    expect(token).toHaveValue('')
    expect(message).toHaveValue('Please check this route.')

    fireEvent.change(token, { target: { value: 'network-token' } })
    await user.click(screen.getByRole('button', { name: /send report/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be submitted/i)
    expect(token).toHaveValue('')
    expect(message).toHaveValue('Please check this route.')
    expect(turnstileReset).toHaveBeenCalledTimes(2)
  })

  it('reports field validation without consuming a valid Turnstile token', async () => {
    vi.restoreAllMocks()
    vi.stubEnv('VITE_TURNSTILE_SITE_KEY', 'test-site-key')
    mockApi(true)
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const turnstileReset = vi.fn()
    vi.stubGlobal('turnstile', {
      render: vi.fn(() => 'test-widget'),
      reset: turnstileReset,
    })
    const user = userEvent.setup()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /south ridge loop/i }))
    await user.click(await screen.findByRole('button', { name: /report a problem/i }))
    const token = document.querySelector<HTMLInputElement>('input[name="turnstile_token"]')!
    fireEvent.change(token, { target: { value: 'still-valid-token' } })
    await user.click(screen.getByRole('button', { name: /send report/i }))
    expect(screen.getByRole('alert')).toHaveTextContent(/check the required fields/i)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(turnstileReset).not.toHaveBeenCalled()
    expect(token).toHaveValue('still-valid-token')
  })
})
