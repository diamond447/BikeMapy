import type { StyleSpecification } from 'maplibre-gl'

export const DEFAULT_VIEW = { longitude: 16.6, latitude: 49.2, zoom: 7.5 }

const OSM_TILE_ORIGIN = 'https://tile.openstreetmap.org'
const CYCLING_TILE_ORIGIN = 'https://tile.waymarkedtrails.org'

/**
 * The OpenStreetMap standard map (OSM Carto) as raster tiles from the OpenStreetMap
 * Foundation, with Waymarked Trails' transparent overlay of signed cycle routes on top.
 * Both ask for visible attribution and no bulk downloads; the OSMF policy also asks for a
 * Referer, which the attribution control and the Referrer-Policy provide. The app draws
 * only lines and fills over it, so the style needs no glyphs or sprites.
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
    cycling: {
      type: 'raster',
      tiles: [`${CYCLING_TILE_ORIGIN}/cycling/{z}/{x}/{y}.png`],
      tileSize: 256,
      maxzoom: 18,
    },
  },
  layers: [
    { id: 'osm-standard', type: 'raster', source: 'osm' },
    { id: 'cycle-routes', type: 'raster', source: 'cycling', paint: { 'raster-opacity': 0.8 } },
  ],
}

const styleUrl = import.meta.env.VITE_MAP_STYLE_URL?.trim()

/** Keep tile style and attribution replaceable without changing the discovery UI. */
export const MAP_PROVIDER = {
  style: styleUrl || OSM_STANDARD_STYLE,
  /** Where the first map requests go, for a preconnect hint. */
  origin: styleUrl || OSM_TILE_ORIGIN,
  attribution:
    import.meta.env.VITE_MAP_ATTRIBUTION?.trim() ||
    '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap contributors</a> · <a href="https://cycling.waymarkedtrails.org/" target="_blank" rel="noreferrer">Waymarked Trails</a>',
}
