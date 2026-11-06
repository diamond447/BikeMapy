import { describe, expect, it } from 'vitest'

import { snapToTileGrid, type Bounds } from './viewportGrid'

const prague: Bounds = [14.35, 50.03, 14.52, 50.12]

describe('snapToTileGrid', () => {
  it('expands the viewport outward to whole tiles', () => {
    const [west, south, east, north] = snapToTileGrid(prague, 12)
    expect(west).toBeLessThanOrEqual(prague[0])
    expect(south).toBeLessThanOrEqual(prague[1])
    expect(east).toBeGreaterThanOrEqual(prague[2])
    expect(north).toBeGreaterThanOrEqual(prague[3])
    // One z13 tile is 360 / 8192 degrees wide.
    const tileWidth = 360 / 2 ** 13
    expect((west + 180) / tileWidth).toBeCloseTo(Math.round((west + 180) / tileWidth), 9)
    expect((east - west) / tileWidth).toBeCloseTo(Math.round((east - west) / tileWidth), 9)
  })

  it('gives small pans inside the same tiles the same request', () => {
    const snapped = snapToTileGrid(prague, 12)
    // Keep every edge well inside its tile (a z13 tile is about 0.044 degrees
    // wide and 0.028 degrees tall here), then pan by less than that margin.
    const inner: Bounds = [
      snapped[0] + 0.01,
      snapped[1] + 0.006,
      snapped[2] - 0.01,
      snapped[3] - 0.006,
    ]
    const panned: Bounds = [inner[0] + 0.004, inner[1] - 0.003, inner[2] + 0.004, inner[3] - 0.003]
    expect(snapToTileGrid(inner, 12)).toEqual(snapped)
    expect(snapToTileGrid(panned, 12)).toEqual(snapped)
  })

  it('keeps the raw bounds when snapping would exceed the API span limit', () => {
    const continent: Bounds = [-10, 30, 75, 70]
    expect(snapToTileGrid(continent, 2)).toEqual(continent)
  })

  it('clamps bounds that MapLibre reports beyond the antimeridian or the poles', () => {
    const [west, south, east, north] = snapToTileGrid([-190, -88, -170, -80], 6)
    expect(west).toBeGreaterThanOrEqual(-180)
    expect(east).toBeLessThanOrEqual(180)
    expect(south).toBeGreaterThanOrEqual(-85.06)
    expect(north).toBeLessThanOrEqual(85.06)
    expect(west).toBeLessThan(east)
    expect(south).toBeLessThan(north)
  })
})
