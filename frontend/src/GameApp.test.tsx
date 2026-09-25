import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { MockMap } = vi.hoisted(() => {
  class MockMap {
    handlers = new Map<string, Set<(event?: unknown) => void>>()
    sources = new Map<string, { setData: ReturnType<typeof vi.fn> }>()

    constructor() {
      // MapLibre emits load and idle asynchronously after construction.
      setTimeout(() => this.emit('load'), 0)
    }

    on(event: string, handler: (event?: unknown) => void) {
      const handlers = this.handlers.get(event) ?? new Set()
      handlers.add(handler)
      this.handlers.set(event, handlers)
      if (event === 'idle') setTimeout(() => this.emit('idle'), 0)
      return this
    }

    once(event: string, handler: (event?: unknown) => void) {
      const wrapped = (value?: unknown) => {
        this.off(event, wrapped)
        handler(value)
      }
      return this.on(event, wrapped)
    }

    emit(event: string, value?: unknown) {
      this.handlers.get(event)?.forEach((handler) => handler(value))
    }

    off(event: string, handler: (event?: unknown) => void) {
      this.handlers.get(event)?.delete(handler)
      return this
    }

    addControl() {
      return this
    }

    addSource(id: string) {
      this.sources.set(id, { setData: vi.fn() })
      return this
    }

    getSource(id: string) {
      return this.sources.get(id)
    }

    addLayer() {
      return this
    }

    setFilter = vi.fn()

    getBounds() {
      return { getWest: () => 14, getSouth: () => 48.5, getEast: () => 19, getNorth: () => 51.2 }
    }

    getZoom() {
      return 7.5
    }

    getCanvas() {
      return { style: {}, focus: vi.fn() }
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

import {
  featureCollection,
  interactionFeatureCollection,
  memberFeatureCollection,
  memberFilter,
  visibleMapData,
} from './GameApp'
import { apiClient } from './api/client'
import { translations } from './i18n/translations'
import GameApp from './GameApp'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('private game map presentation', () => {
  it('keeps trace metadata to member color and calendar date', () => {
    const result = featureCollection(
      [
        {
          id: 'activity-1',
          player_id: 7,
          calendar_date: '2026-09-21',
          geometry: {
            type: 'LineString',
            coordinates: [
              [14, 49],
              [14.1, 49.1],
            ],
          },
        },
      ],
      new Map([[7, '#F4B942']]),
    )
    expect(result.features[0].properties).toEqual({
      player_id: 7,
      calendar_date: '2026-09-21',
      color: '#F4B942',
    })
    expect(result.features[0]).not.toHaveProperty('title')
    expect(result.features[0]).not.toHaveProperty('started_at')
  })

  it('filters cached map data by member without changing trace payloads', () => {
    const data = {
      status: 'loaded' as const,
      competition_id: 'competition-1',
      members: [
        { player_id: 7, display_name: 'Seven', nickname: null, color: '#F4B942', is_owner: true },
        { player_id: 8, display_name: 'Eight', nickname: null, color: '#2B8C76', is_owner: false },
      ],
      activities: [
        {
          id: 'activity-1',
          player_id: 7,
          calendar_date: '2026-09-21',
          geometry: {
            type: 'LineString' as const,
            coordinates: [
              [14, 49],
              [14.1, 49.1],
            ],
          },
        },
        {
          id: 'activity-2',
          player_id: 8,
          calendar_date: '2026-09-22',
          geometry: {
            type: 'LineString' as const,
            coordinates: [
              [15, 49],
              [15.1, 49.1],
            ],
          },
        },
      ],
      truncated: false,
      limits: { max_features: 1200, max_coordinates: 120000 },
      bounds: { west: 14, south: 48, east: 16, north: 51 },
    }
    const result = visibleMapData(data, [8])
    expect(result.members.map((member) => member.player_id)).toEqual([8])
    expect(result.activities.map((activity) => activity.id)).toEqual(['activity-2'])
  })

  it('groups visual geometry by member while retaining all line parts', () => {
    const result = memberFeatureCollection([
      {
        id: 'activity-1',
        player_id: 7,
        calendar_date: '2026-09-21',
        geometry: {
          type: 'LineString' as const,
          coordinates: [
            [14, 49],
            [14.1, 49.1],
          ],
        },
      },
      {
        id: 'activity-2',
        player_id: 7,
        calendar_date: '2026-09-22',
        geometry: {
          type: 'MultiLineString' as const,
          coordinates: [
            [
              [15, 49],
              [15.1, 49.1],
            ],
          ],
        },
      },
    ])
    expect(result.features).toHaveLength(1)
    expect(result.features[0].geometry.coordinates).toHaveLength(2)
  })

  it('filters both visible and interaction features to exactly the selected members', () => {
    expect(memberFilter([7, 11])).toEqual(['match', ['get', 'player_id'], [7, 11], true, false])
    expect(memberFilter([])).toEqual(['==', ['get', 'player_id'], -1])
  })

  it('keeps the full interaction source activity-level but privacy-minimal', () => {
    const result = interactionFeatureCollection([
      {
        id: 'activity-1',
        player_id: 7,
        calendar_date: '2026-09-21',
        geometry: {
          type: 'LineString' as const,
          coordinates: [
            [14, 49],
            [14.1, 49.1],
          ],
        },
      },
    ])
    expect(result.features).toHaveLength(1)
    expect(result.features[0].properties).toEqual({ player_id: 7 })
  })

  it('loads, filters, and clears the private map through the visible controls', async () => {
    const competition = {
      id: 'competition-1',
      name: 'Dawn rides',
      invite_code: 'DAWN234567',
      owner_player_id: 7,
      is_owner: true,
      is_active: true,
      is_selected: true,
      color: '#F4B942',
      created_at: '2026-09-21T00:00:00Z',
      sharing_scope: 'recent',
      members: [
        { player_id: 7, display_name: 'Rider', nickname: null, color: '#F4B942', is_owner: true },
      ],
    }
    const response = {
      status: 'loaded' as const,
      competition_id: competition.id,
      members: competition.members,
      activities: [
        {
          id: 'activity-1',
          player_id: 7,
          calendar_date: '2026-09-21',
          geometry: {
            type: 'LineString' as const,
            coordinates: [
              [14, 49],
              [14.1, 49.1],
            ],
          },
        },
      ],
      truncated: false,
      limits: { max_features: 1200, max_coordinates: 120000 },
    }
    vi.spyOn(apiClient, 'GET').mockImplementation(((path: string) => {
      if (path.includes('/map/'))
        return Promise.resolve({ data: response, response: new Response() })
      return Promise.resolve({
        data: { competitions: [competition], active_competition_id: competition.id },
        response: new Response(),
      })
    }) as never)

    const user = userEvent.setup()
    render(<GameApp />)
    await waitFor(() =>
      expect(document.querySelector('.game-map-stage')).toHaveAttribute(
        'data-map-response-loaded',
        'true',
      ),
    )
    await user.click(screen.getByRole('button', { name: '2026-09-21' }))
    expect(screen.getByRole('complementary', { name: 'Trace detail' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /close trace detail/i }))
    expect(screen.queryByRole('complementary', { name: 'Trace detail' })).not.toBeInTheDocument()
    const member = screen.getByRole('checkbox')
    await user.click(member)
    expect(member).not.toBeChecked()
    expect(translations.en.gameMapTitle).toBe('Ride together, privately.')
  })
})
