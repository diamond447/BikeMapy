export const DEFAULT_VIEW = { longitude: 16.6, latitude: 49.2, zoom: 7.5 }

/** Keep tile style and attribution replaceable without changing the discovery UI. */
export const MAP_PROVIDER = {
  style: import.meta.env.VITE_MAP_STYLE_URL ?? 'https://tiles.openfreemap.org/styles/liberty',
  attribution:
    import.meta.env.VITE_MAP_ATTRIBUTION ?? '© OpenFreeMap · © OpenStreetMap contributors',
}
