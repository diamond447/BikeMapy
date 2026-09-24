import { describe, expect, it } from 'vitest'

import { featureCollection } from './GameApp'

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
})
