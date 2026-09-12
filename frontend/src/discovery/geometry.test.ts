import { describe, expect, it } from 'vitest'

import { geometryBounds, geometryCoordinates } from './geometry'

const geometry = {
  type: 'LineString' as const,
  coordinates: [
    [16, 49],
    [16.2, 49.4],
    [16.8, 49.1],
  ],
}

describe('route geometry helpers', () => {
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

  it('frames a single point and ignores malformed coordinates', () => {
    expect(geometryBounds({ type: 'Point', coordinates: [16.5, 49.2] })).toEqual([
      [16.5, 49.2],
      [16.5, 49.2],
    ])
    expect(
      geometryBounds({
        type: 'MultiLineString',
        coordinates: [
          [],
          [
            [16, 49],
            ['invalid', 50],
            [Number.NaN, 50],
          ],
          null,
        ],
      } as never),
    ).toEqual([
      [16, 49],
      [16, 49],
    ])
    expect(geometryBounds({ type: 'LineString', coordinates: [] })).toBeNull()
  })
})
