type MapLibreModule = typeof import('maplibre-gl')

let pending: Promise<MapLibreModule> | null = null

/**
 * Load MapLibre and its stylesheet once for every map on the page.
 *
 * The library, its worker and its 83 kB stylesheet stay out of the initial
 * bundle; main.tsx calls this before the first render so the download runs
 * in parallel with React instead of starting from the map effect. A failed
 * load is forgotten so that a retry downloads again.
 */
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
