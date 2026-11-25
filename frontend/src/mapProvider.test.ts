import { describe, expect, it } from 'vitest'

import { MAP_PROVIDER } from './mapProvider'

describe('map provider', () => {
  it('uses clickable official attribution links by default', () => {
    expect(MAP_PROVIDER.attribution).toContain('href="https://www.openstreetmap.org/copyright"')
    expect(MAP_PROVIDER.attribution).toContain('OpenStreetMap contributors')
    expect(MAP_PROVIDER.attribution).toContain('href="https://cycling.waymarkedtrails.org/"')
    expect(MAP_PROVIDER.attribution).not.toContain('openfreemap')
  })

  it('shows the OpenStreetMap standard map by default', () => {
    expect(MAP_PROVIDER.origin).toBe('https://tile.openstreetmap.org')
    expect(MAP_PROVIDER.style).toMatchObject({
      version: 8,
      sources: {
        osm: {
          type: 'raster',
          tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
          tileSize: 256,
        },
      },
    })
  })

  it('draws the Waymarked Trails cycle routes over the basemap', () => {
    expect(MAP_PROVIDER.style).toMatchObject({
      sources: {
        cycling: {
          type: 'raster',
          tiles: ['https://tile.waymarkedtrails.org/cycling/{z}/{x}/{y}.png'],
        },
      },
      layers: [
        { id: 'osm-standard', source: 'osm' },
        {
          id: 'cycle-routes',
          source: 'cycling',
          paint: { 'raster-opacity': 0.55, 'raster-saturation': -0.6 },
        },
      ],
    })
  })
})
