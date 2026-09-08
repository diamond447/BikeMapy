import { describe, expect, it } from 'vitest'

import { MAP_PROVIDER } from './mapProvider'

describe('map provider attribution', () => {
  it('uses clickable official attribution links by default', () => {
    expect(MAP_PROVIDER.attribution).toContain('href="https://openfreemap.org/"')
    expect(MAP_PROVIDER.attribution).toContain('href="https://www.openstreetmap.org/copyright"')
    expect(MAP_PROVIDER.attribution).toContain('OpenStreetMap contributors')
  })
})
