/** Snap a viewport outward to the tile grid one zoom below, so small pans reuse a URL. */

export type Bounds = [west: number, south: number, east: number, north: number]

const MAX_LATITUDE = 85.05112878
/** The API rejects wider or taller viewports; keep the raw bounds then. */
const MAX_SPAN_DEGREES = 90

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value))
}

function tileX(longitude: number, scale: number) {
  return Math.floor(((longitude + 180) / 360) * scale)
}

function tileY(latitude: number, scale: number) {
  const radians = (clamp(latitude, -MAX_LATITUDE, MAX_LATITUDE) * Math.PI) / 180
  return Math.floor(((1 - Math.asinh(Math.tan(radians)) / Math.PI) / 2) * scale)
}

function tileLongitude(x: number, scale: number) {
  return (x / scale) * 360 - 180
}

function tileLatitude(y: number, scale: number) {
  return (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / scale))) * 180) / Math.PI
}

export function snapToTileGrid(bounds: Bounds, zoom: number): Bounds {
  const west = clamp(bounds[0], -180, 180)
  const east = clamp(bounds[2], -180, 180)
  const south = clamp(bounds[1], -MAX_LATITUDE, MAX_LATITUDE)
  const north = clamp(bounds[3], -MAX_LATITUDE, MAX_LATITUDE)
  if (!(west < east && south < north)) return [west, south, east, north]
  const gridZoom = clamp(Math.round(zoom) + 1, 0, 22)
  const scale = 2 ** gridZoom
  const minimumX = clamp(tileX(west, scale), 0, scale - 1)
  const maximumX = clamp(tileX(east, scale), 0, scale - 1)
  const minimumY = clamp(tileY(north, scale), 0, scale - 1)
  const maximumY = clamp(tileY(south, scale), 0, scale - 1)
  const snapped: Bounds = [
    tileLongitude(minimumX, scale),
    tileLatitude(maximumY + 1, scale),
    tileLongitude(maximumX + 1, scale),
    tileLatitude(minimumY, scale),
  ]
  if (snapped[2] - snapped[0] > MAX_SPAN_DEGREES || snapped[3] - snapped[1] > MAX_SPAN_DEGREES)
    return [west, south, east, north]
  return snapped
}
