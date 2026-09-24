import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { MockMap } = vi.hoisted(() => {
  class MockMap {
    static last: MockMap | undefined
    sources: Record<string, { setData: ReturnType<typeof vi.fn> }> = {}
    images = new Set<string>()
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

    hasImage(id: string) {
      return this.images.has(id)
    }

    addImage(id: string) {
      this.images.add(id)
    }

    getBounds() {
      return { getWest: () => 14, getSouth: () => 48.5, getEast: () => 19, getNorth: () => 51.2 }
    }

    getZoom() {
      return 7.5
    }

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

import { apiClient } from '../api/client'
import { CaptureDashboard } from './CaptureDashboard'
import { translations } from '../i18n/translations'

const get = apiClient.GET as unknown as ReturnType<typeof vi.fn>
const copy = translations.en
const competition = { id: 'competition-1', name: 'Weekend crew', members: [] }
const capture = {
  status: 'fresh',
  is_final: true,
  competition_id: competition.id,
  generation: 3,
  snapshot_generation: 3,
  calculated_at: '2026-09-22T12:00:00Z',
  faces: [
    {
      id: 9,
      geometry: {
        type: 'Polygon',
        coordinates: [
          [
            [14, 49],
            [14.1, 49],
            [14.1, 49.1],
            [14, 49],
          ],
        ],
      },
      area_m2: '100.000',
      effective_date: '2026-09-21',
      shared: true,
      owners: [
        {
          player_id: 7,
          display_name: 'Rider one',
          nickname: null,
          color: '#D34D32',
          shared_area_m2: '50.000',
        },
        {
          player_id: 8,
          display_name: 'Rider two',
          nickname: null,
          color: '#527A66',
          shared_area_m2: '50.000',
        },
      ],
    },
  ],
  members: [
    {
      player_id: 7,
      display_name: 'Rider one',
      nickname: null,
      color: '#D34D32',
      is_owner: true,
      area_m2: '50.000',
      rank: 1,
      monthly_net_change_m2: [{ month: '2026-09', net_change_m2: '12.300' }],
    },
    {
      player_id: 8,
      display_name: 'Rider two',
      nickname: null,
      color: '#527A66',
      is_owner: false,
      area_m2: '50.000',
      rank: 2,
      monthly_net_change_m2: [{ month: '2026-09', net_change_m2: '-4.500' }],
    },
  ],
  help: {},
  limits: { max_faces: 1200, max_response_bytes: 4000000 },
}

function apiResult(data: unknown) {
  return { response: new Response(null, { status: 200 }), data }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  get.mockResolvedValue(apiResult(capture))
})

describe('capture territory presentation', () => {
  it('keeps all global ranks visible and renders multi-owner hatch data', async () => {
    render(
      <CaptureDashboard
        copy={copy}
        competitions={[competition as never]}
        competitionId={competition.id}
        setCompetitionId={vi.fn()}
        signedOut={false}
      />,
    )

    await screen.findByText('Area leaderboard')
    expect(document.querySelectorAll('.capture-rider small')[0]).toHaveTextContent('+12.3 m²')
    expect(document.querySelectorAll('.capture-rider small')[1]).toHaveTextContent('-4.5 m²')
    expect(screen.getAllByText('Rider two')).not.toHaveLength(0)
    const source = MockMap.last?.sources['capture-territory']?.setData
    await waitFor(() => expect(source).toHaveBeenCalled())
    expect(source?.mock.lastCall?.[0].features[0].properties.pattern).toContain('capture-hatch')

    fireEvent.click(screen.getByRole('checkbox', { name: /Rider two/i }))
    expect(screen.getAllByText('Rider two')).not.toHaveLength(0)
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith(
      '/api/v1/game/competitions/{competition_id}/capture/',
      expect.objectContaining({
        params: expect.objectContaining({ query: expect.objectContaining({ member: [7] }) }),
      }),
    )
  })

  it('announces pending and failed results without hiding the last leaderboard', async () => {
    get.mockResolvedValueOnce(apiResult({ ...capture, status: 'pending', is_final: false }))
    render(
      <CaptureDashboard
        copy={copy}
        competitions={[competition as never]}
        competitionId={competition.id}
        setCompetitionId={vi.fn()}
        signedOut={false}
      />,
    )
    expect(await screen.findByText(copy.gameCapturePending)).toBeInTheDocument()
    expect(screen.getAllByText('Rider one')).not.toHaveLength(0)

    cleanup()
    get.mockResolvedValueOnce(apiResult({ ...capture, status: 'failed', is_final: false }))
    render(
      <CaptureDashboard
        copy={copy}
        competitions={[competition as never]}
        competitionId={competition.id}
        setCompetitionId={vi.fn()}
        signedOut={false}
      />,
    )
    expect(await screen.findByText(copy.gameCaptureFailed)).toBeInTheDocument()
  })
})
