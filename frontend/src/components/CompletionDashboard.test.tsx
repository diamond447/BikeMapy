import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { MockMap } = vi.hoisted(() => {
  class MockMap {
    static last: MockMap | undefined
    sources: Record<string, { setData: ReturnType<typeof vi.fn> }> = {}
    handlers: Record<string, () => void> = {}

    constructor() {
      MockMap.last = this
    }

    on(event: string, handler: () => void) {
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

    addLayer() {
      return this
    }

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

vi.mock('../api/client', () => ({
  apiClient: { GET: vi.fn() },
  rememberCsrfToken: vi.fn(),
}))

import { completionFeature, completionPercent } from './CompletionDashboard'
import { CompletionDashboard } from './CompletionDashboard'
import { apiClient } from '../api/client'
import { translations } from '../i18n/translations'

const get = apiClient.GET as unknown as ReturnType<typeof vi.fn>
const copy = translations.en
const routeId = '33333333-3333-4333-8333-333333333333'
const stageId = '44444444-4444-4444-8444-444444444444'
const geometry = {
  type: 'LineString',
  coordinates: [
    [14, 49],
    [14.2, 49.2],
  ],
}

const projection = (status = 'fresh', partial = false) => ({
  status,
  total_length_meters: '20000.000',
  covered_length_meters: status === 'fresh' ? '7400.000' : '0.000',
  completion_percent: status === 'fresh' ? '37.000' : '0.000',
  calculated_at: status === 'fresh' ? '2026-09-21T00:00:00Z' : null,
  error: status === 'failed' ? 'worker unavailable' : '',
  covered_geometry: status === 'fresh' ? geometry : null,
  monthly:
    status === 'fresh'
      ? [
          { month: '2026-08-01', covered_length_meters: '9999.000' },
          { month: '2026-09-01', covered_length_meters: '7400.000' },
        ]
      : [],
  partial,
})

const route = {
  id: routeId,
  source_identifier: 'vc-1',
  route_number: '1',
  title: 'Via Czechia north',
  operator: 'Via Czechia',
  network: 'via-czechia',
  publication_status: 'approved',
  source_kind: 'via_czechia',
  attribution: { attribution_text: 'Via Czechia' },
}

const detail = {
  route_id: routeId,
  version: 1,
  title: route.title,
  route_number: route.route_number,
  source_kind: route.source_kind,
  geometry,
  attribution: { attribution_text: 'Via Czechia', licence: 'ODbL' },
  player: projection(),
  competition: projection('pending'),
  stages: [
    {
      route_id: stageId,
      version: 1,
      title: 'North stage',
      route_number: '1A',
      geometry,
      player: projection(),
      competition: projection('failed', true),
    },
  ],
}

function apiResult(data: unknown) {
  return { response: new Response(null, { status: 200 }), data }
}

afterEach(() => {
  cleanup()
  sessionStorage.clear()
  vi.clearAllMocks()
})

beforeEach(() => {
  get.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes('/completion/')
        ? apiResult(detail)
        : apiResult({ next: null, previous: null, results: [route] }),
    ),
  )
})

describe('official route completion presentation', () => {
  it('keeps the reference geometry and coverage as separate map features', () => {
    const geometry = {
      type: 'LineString',
      coordinates: [
        [14, 50],
        [14.1, 50.1],
      ],
    }
    expect(completionFeature(geometry).geometry).toEqual(geometry)
    expect(completionFeature(geometry, { state: 'covered' }).properties).toEqual({
      state: 'covered',
    })
  })

  it('formats measured completion percentages without hiding pending state', () => {
    expect(completionPercent({ completion_percent: '37.456' } as never)).toBe('37.5%')
    expect(completionPercent(null)).toBe('—')
  })

  it('loads official routes, preserves selection, and switches player/group projections', async () => {
    render(
      <CompletionDashboard
        copy={copy}
        competitions={[{ id: 'competition-1', name: 'Weekend crew', members: [] } as never]}
        competitionId="competition-1"
        setCompetitionId={vi.fn()}
        signedOut={false}
      />,
    )

    const routeButton = await screen.findByRole('button', { name: /1 Via Czechia north/ })
    expect((await screen.findAllByText('37.0%')).length).toBeGreaterThan(0)
    fireEvent.click(routeButton)
    const stageButton = await screen.findByRole('button', { name: /1A North stage/ })
    fireEvent.click(stageButton)
    fireEvent.click(screen.getByRole('tab', { name: 'Competition' }))
    await waitFor(() =>
      expect(document.querySelector('.completion-status')).toHaveTextContent('Calculation failed'),
    )
    expect(screen.queryByText('worker unavailable')).not.toBeInTheDocument()
    expect(screen.getByText(copy.gameCompletionPartial)).toBeInTheDocument()
    expect(screen.getByText('Via Czechia · ODbL')).toBeInTheDocument()
    expect(sessionStorage.getItem('bikemapy:game-completion')).toContain(stageId)
    expect(MockMap.last?.fitBounds).toHaveBeenCalled()
    expect(MockMap.last?.sources['reference-covered']?.setData).toHaveBeenCalled()
  })

  it('explains signed-out and failed route states with a retry action', async () => {
    render(
      <CompletionDashboard copy={copy} competitions={[]} setCompetitionId={vi.fn()} signedOut />,
    )
    expect(screen.getByText(copy.gameMapSignedOut)).toBeInTheDocument()

    cleanup()
    get.mockReset()
    get.mockRejectedValueOnce(new Error('offline'))
    render(
      <CompletionDashboard
        copy={copy}
        competitions={[{ id: 'competition-1', name: 'Weekend crew', members: [] } as never]}
        competitionId="competition-1"
        setCompetitionId={vi.fn()}
        signedOut={false}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(copy.gameCompletionError)
    get.mockResolvedValueOnce(apiResult({ next: null, previous: null, results: [] }))
    fireEvent.click(screen.getByRole('button', { name: copy.gameCompletionRetry }))
    expect(await screen.findByText(copy.gameCompletionEmpty)).toBeInTheDocument()
  })

  it('loads the complete bounded route catalogue through cursor pages', async () => {
    const routes = [
      route,
      ...Array.from({ length: 100 }, (_, index) => ({
        ...route,
        id: `route-${index + 2}`,
        route_number: String(index + 2),
        title: `Via Czechia ${index + 2}`,
      })),
    ]
    get.mockImplementation(
      (path: string, options?: { params?: { query?: { cursor?: string } } }) => {
        if (path.includes('/completion/')) return Promise.resolve(apiResult(detail))
        const cursor = options?.params?.query?.cursor
        return Promise.resolve(
          apiResult({
            next: cursor ? null : 'http://localhost/api/v1/game/reference-routes/?cursor=page-2',
            previous: null,
            results: cursor ? routes.slice(100) : routes.slice(0, 100),
          }),
        )
      },
    )
    render(
      <CompletionDashboard
        copy={copy}
        competitions={[{ id: 'competition-1', name: 'Weekend crew', members: [] } as never]}
        competitionId="competition-1"
        setCompetitionId={vi.fn()}
        signedOut={false}
      />,
    )
    expect(await screen.findByRole('button', { name: /101 Via Czechia 101/ })).toBeInTheDocument()
    expect(
      get.mock.calls.filter(([path]) => path === '/api/v1/game/reference-routes/'),
    ).toHaveLength(2)
  })

  it('announces when the safe route page bound leaves the catalogue incomplete', async () => {
    const routes = Array.from({ length: 1000 }, (_, index) => ({
      ...route,
      id: `bounded-route-${index + 1}`,
      route_number: String(index + 1),
      title: `Bounded route ${index + 1}`,
    }))
    get.mockImplementation(
      (path: string, options?: { params?: { query?: { cursor?: string } } }) => {
        if (path.includes('/completion/')) return Promise.resolve(apiResult(detail))
        const cursor = options?.params?.query?.cursor
        const page = cursor ? Number(cursor.replace('page-', '')) : 1
        return Promise.resolve(
          apiResult({
            next: `http://localhost/api/v1/game/reference-routes/?cursor=page-${page + 1}`,
            previous: null,
            results: routes.slice((page - 1) * 100, page * 100),
          }),
        )
      },
    )
    render(
      <CompletionDashboard
        copy={copy}
        competitions={[{ id: 'competition-1', name: 'Weekend crew', members: [] } as never]}
        competitionId="competition-1"
        setCompetitionId={vi.fn()}
        signedOut={false}
      />,
    )
    expect(
      await screen.findByRole('button', { name: /1000 Bounded route 1000/ }),
    ).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      copy.gameCompletionCatalogueIncomplete,
    )
    expect(
      get.mock.calls.filter(([path]) => path === '/api/v1/game/reference-routes/'),
    ).toHaveLength(10)
  })

  it('keeps the latest route list and detail after delayed competition remounts', async () => {
    const routeA = { ...route, id: 'route-a', title: 'Route A' }
    const routeB = { ...route, id: 'route-b', title: 'Route B' }
    const pendingLists = new Map<
      string,
      Array<{ resolve: (value: unknown) => void; reject: (error: Error) => void }>
    >()
    get.mockImplementation(
      (path: string, options?: { params?: { query?: { competition_id?: string } } }) => {
        if (path.includes('/completion/')) {
          const selected =
            options?.params?.query?.competition_id === 'competition-b' ? routeB : routeA
          return Promise.resolve(
            apiResult({ ...detail, route_id: selected.id, title: selected.title }),
          )
        }
        const competitionId = options?.params?.query?.competition_id ?? ''
        return new Promise((resolve, reject) => {
          const pending = pendingLists.get(competitionId) ?? []
          pending.push({ resolve, reject })
          pendingLists.set(competitionId, pending)
        })
      },
    )
    const props = {
      copy,
      competitions: [
        { id: 'competition-a', name: 'A', members: [] },
        { id: 'competition-b', name: 'B', members: [] },
      ] as never,
      setCompetitionId: vi.fn(),
      signedOut: false,
    }
    const { rerender } = render(<CompletionDashboard {...props} competitionId="competition-a" />)
    await waitFor(() => expect(pendingLists.get('competition-a')).toHaveLength(1))
    rerender(<CompletionDashboard {...props} competitionId="competition-b" />)
    await waitFor(() => expect(pendingLists.get('competition-b')).toHaveLength(1))
    pendingLists
      .get('competition-b')?.[0]
      .resolve(apiResult({ next: null, previous: null, results: [routeB] }))
    expect(await screen.findByRole('button', { name: /1 Route B/ })).toBeInTheDocument()
    pendingLists.get('competition-a')?.[0].reject(new Error('stale list'))
    expect(await screen.findByText('Route B')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    rerender(<CompletionDashboard {...props} competitionId="competition-a" />)
    await waitFor(() => expect(pendingLists.get('competition-a')).toHaveLength(2))
    pendingLists
      .get('competition-a')?.[1]
      .resolve(apiResult({ next: null, previous: null, results: [routeA] }))
    expect(await screen.findByRole('button', { name: /1 Route A/ })).toBeInTheDocument()
    expect((await screen.findAllByText('37.0%')).length).toBeGreaterThan(0)
  })
})
