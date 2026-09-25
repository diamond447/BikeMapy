import { describe, expect, it } from 'vitest'

import { featureCollection, memberFeatureCollection, visibleMapData } from './GameApp'

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
})
