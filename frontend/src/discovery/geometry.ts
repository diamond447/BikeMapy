import type { Geometry } from './types'

export function geometryCoordinates(geometry: Geometry | null): number[][] {
  if (!geometry) return []
  const walk = (value: unknown): number[][] => {
    if (!Array.isArray(value)) return []
    if (value.length >= 2 && typeof value[0] === 'number' && typeof value[1] === 'number')
      return [[value[0], value[1]]]
    return value.flatMap(walk)
  }
  return walk(geometry.coordinates)
}

export function geometryBounds(
  geometry: Geometry | null,
): [[number, number], [number, number]] | null {
  let west = Infinity
  let south = Infinity
  let east = -Infinity
  let north = -Infinity
  let found = false

  const visit = (value: unknown): void => {
    if (!Array.isArray(value)) return
    const longitude = value[0]
    const latitude = value[1]
    if (
      value.length >= 2 &&
      typeof longitude === 'number' &&
      Number.isFinite(longitude) &&
      typeof latitude === 'number' &&
      Number.isFinite(latitude)
    ) {
      found = true
      west = Math.min(west, longitude)
      south = Math.min(south, latitude)
      east = Math.max(east, longitude)
      north = Math.max(north, latitude)
      return
    }
    value.forEach(visit)
  }

  visit(geometry?.coordinates)
  return found
    ? [
        [west, south],
        [east, north],
      ]
    : null
}
