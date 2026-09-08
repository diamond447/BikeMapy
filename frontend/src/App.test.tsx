import { cleanup, render, screen, waitFor } from '@testing-library/react'
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
}))

import type { components } from './api/generated/schema'
import { apiClient } from './api/client'
import App, { geometryBounds, geometryCoordinates, parseState, writeUrl } from './App'

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
  sources: [],
  variants: [],
  geometry: null,
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

  it('hydrates filters, viewport, and selected route from a shared URL', () => {
    window.history.replaceState(
      {},
      '',
      `/?q=forest&author=Jana&lng=17.1&lat=49.3&z=11&route=${route.id}`,
    )
    const state = parseState()
    expect(state.filters.search).toBe('forest')
    expect(state.filters.author).toBe('Jana')
    expect(state.view.zoom).toBe(11)
    expect(state.routeId).toBe(route.id)
  })

  it('uses defaults for missing URL values and does not leak local state into shared links', () => {
    localStorage.setItem(
      'bikemapy:discovery-state',
      JSON.stringify({
        filters: { search: 'private' },
        routeId: route.id,
        view: { longitude: 1, latitude: 2, zoom: 3 },
      }),
    )
    window.history.replaceState({}, '', '/?q=shared')
    const state = parseState()
    expect(state.filters.search).toBe('shared')
    expect(state.routeId).toBeNull()
    expect(state.view).toEqual(
      expect.objectContaining({ longitude: 16.6, latitude: 49.2, zoom: 7.5 }),
    )
    window.history.replaceState({}, '', '/')
    expect(parseState().filters.search).toBe('private')
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
    const sheetToggle = screen.getByRole('button', { name: /show routes/i })
    expect(sheetToggle).toBeDisabled()
    const collapsedCall = MockMap.last?.fitBounds.mock.calls.at(-1)
    expect(collapsedCall?.[1].padding.bottom).toBe(42 + 150 + 24)
    expect(80 + Number(collapsedCall?.[1].padding.bottom)).toBeLessThan(844)

    await user.click(screen.getByRole('button', { name: /close route details/i }))
    expect(screen.getByRole('button', { name: /show routes/i })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /show routes/i }))
    expect(screen.getByRole('button', { name: /hide routes/i })).toBeInTheDocument()
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

  it('covers numeric filters, history navigation, and retry', async () => {
    vi.restoreAllMocks()
    mockApi(true)
    const user = userEvent.setup()
    render(<App />)
    await user.type(await screen.findByLabelText('Distance from (m)'), '1000')
    await user.type(screen.getByLabelText('Distance to (m)'), '80000')
    await user.type(screen.getByLabelText('Climb from (m)'), '100')
    await user.type(screen.getByLabelText('Climb to (m)'), '1000')
    await user.type(screen.getByLabelText('Category'), 'gravel')
    expect(window.location.search).toContain('category=gravel')
    window.dispatchEvent(new PopStateEvent('popstate'))
    vi.restoreAllMocks()
    mockApi()
    render(<App />)
    await user.click(await screen.findByRole('button', { name: /^retry$/i }))
  })

  it('walks nested route geometry and bounds', () => {
    expect(
      geometryCoordinates({
        type: 'MultiLineString',
        coordinates: [
          [
            [2, 3],
            [4, 5],
          ],
          [
            [-1, 8],
            [7, 0],
          ],
        ],
      }),
    ).toHaveLength(4)
    expect(geometryBounds(geometry)).toEqual([
      [16, 49],
      [16.8, 49.4],
    ])
    expect(geometryBounds(null)).toBeNull()
  })
})
