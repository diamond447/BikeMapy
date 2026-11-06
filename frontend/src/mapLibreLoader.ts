type MapLibreModule = typeof import('maplibre-gl')

let pending: Promise<MapLibreModule> | null = null

/** Load MapLibre and its stylesheet once; a failed load is retried next time. */
export function loadMapLibre(): Promise<MapLibreModule> {
  pending ??= Promise.all([import('maplibre-gl'), import('maplibre-gl/dist/maplibre-gl.css')]).then(
    ([maplibregl]) => maplibregl,
    (error: unknown) => {
      pending = null
      throw error
    },
  )
  return pending
}
