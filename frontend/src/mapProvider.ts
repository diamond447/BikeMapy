import type { StyleSpecification } from 'maplibre-gl'

export const DEFAULT_VIEW = { longitude: 16.6, latitude: 49.2, zoom: 7.5 }

const OSM_TILE_ORIGIN = 'https://tile.openstreetmap.org'

/**
 * The OpenStreetMap standard map (OSM Carto) as raster tiles from the OpenStreetMap
 * Foundation. Their tile usage policy asks for visible attribution and a Referer, which
 * the attribution control and the Referrer-Policy provide, and allows no bulk downloads.
 * The app draws only lines and fills over it, so the style needs no glyphs or sprites.
 */
const OSM_STANDARD_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: [`${OSM_TILE_ORIGIN}/{z}/{x}/{y}.png`],
      tileSize: 256,
      maxzoom: 19,
    },
  },
  layers: [{ id: 'osm-standard', type: 'raster', source: 'osm' }],
}

const styleUrl = import.meta.env.VITE_MAP_STYLE_URL?.trim()

/** Keep tile style and attribution replaceable without changing the discovery UI. */
export const MAP_PROVIDER = {
  style: styleUrl || OSM_STANDARD_STYLE,
  /** Where the first map requests go, for a preconnect hint. */
  origin: styleUrl || OSM_TILE_ORIGIN,
  attribution:
    import.meta.env.VITE_MAP_ATTRIBUTION?.trim() ||
    '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap contributors</a>',
}
