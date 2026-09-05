/* eslint-disable react-refresh/only-export-components -- state helpers are exported for interaction tests. */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import type { GeoJSONSource, Map as MapLibreMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import type { components } from './api/generated/schema'
import { apiClient } from './api/client'
import { DEFAULT_VIEW, MAP_PROVIDER } from './mapProvider'

type Route = components['schemas']['Route']
type SpatialRoute = components['schemas']['SpatialRoute']
type ViewportResponse = components['schemas']['ViewportResponse']
type Geometry = NonNullable<SpatialRoute['geometry']>
type Filters = {
  search: string
  author: string
  category: string
  min_distance_m: string
  max_distance_m: string
  min_ascent_m: string
  max_ascent_m: string
}
type ViewState = {
  longitude: number
  latitude: number
  zoom: number
  bounds?: [number, number, number, number]
}
type DiscoveryState = {
  filters: Filters
  view: ViewState
  routeId: string | null
  viewportOnly: boolean
}

const DEFAULT_FILTERS: Filters = {
  search: '',
  author: '',
  category: '',
  min_distance_m: '',
  max_distance_m: '',
  min_ascent_m: '',
  max_ascent_m: '',
}
const STORAGE_KEY = 'bikemapy:discovery-state'
const ROUTE_SOURCE = 'browse-routes'
const HEAT_SOURCE = 'browse-heatmap'
const SELECTED_SOURCE = 'selected-route'

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
  const points = geometryCoordinates(geometry)
  if (!points.length) return null
  const longitudes = points.map((point) => point[0])
  const latitudes = points.map((point) => point[1])
  return [
    [Math.min(...longitudes), Math.min(...latitudes)],
    [Math.max(...longitudes), Math.max(...latitudes)],
  ]
}

export function parseState(): DiscoveryState {
  let saved: Partial<DiscoveryState> = {}
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as typeof saved
  } catch {
    /* malformed preference */
  }
  const params = new URLSearchParams(window.location.search)
  const hasExplicitUrlState = params.toString().length > 0
  const number = (key: string, fallback: number) => {
    const raw = params.get(key)
    if (raw === null || raw.trim() === '') return fallback
    const value = Number(raw)
    return Number.isFinite(value) ? value : fallback
  }
  const aliases: Record<keyof Filters, string> = {
    search: 'q',
    author: 'author',
    category: 'category',
    min_distance_m: 'dmin',
    max_distance_m: 'dmax',
    min_ascent_m: 'emin',
    max_ascent_m: 'emax',
  }
  const hasUrlBounds = ['w', 's', 'e', 'n'].every((key) => params.has(key))
  const bounds = hasUrlBounds
    ? ([number('w', -180), number('s', -85), number('e', 180), number('n', 85)] as [
        number,
        number,
        number,
        number,
      ])
    : undefined
  return {
    filters: (Object.keys(DEFAULT_FILTERS) as (keyof Filters)[]).reduce(
      (result, key) => ({
        ...result,
        [key]:
          params.get(aliases[key]) ?? (hasExplicitUrlState ? '' : (saved.filters?.[key] ?? '')),
      }),
      { ...DEFAULT_FILTERS },
    ),
    view: {
      longitude: number('lng', DEFAULT_VIEW.longitude),
      latitude: number('lat', DEFAULT_VIEW.latitude),
      zoom: number('z', DEFAULT_VIEW.zoom),
      bounds,
    },
    routeId: params.get('route') ?? (hasExplicitUrlState ? null : (saved.routeId ?? null)),
    viewportOnly:
      params.get('inview') === '1' || (!hasExplicitUrlState && saved.viewportOnly === true),
  }
}

export function writeUrl(
  filters: Filters,
  view: ViewState,
  routeId: string | null,
  mode: 'push' | 'replace',
  viewportOnly = false,
) {
  const params = new URLSearchParams()
  const aliases: Record<keyof Filters, string> = {
    search: 'q',
    author: 'author',
    category: 'category',
    min_distance_m: 'dmin',
    max_distance_m: 'dmax',
    min_ascent_m: 'emin',
    max_ascent_m: 'emax',
  }
  ;(Object.keys(aliases) as (keyof Filters)[]).forEach((key) => {
    if (filters[key]) params.set(aliases[key], filters[key])
  })
  params.set('lng', view.longitude.toFixed(4))
  params.set('lat', view.latitude.toFixed(4))
  params.set('z', view.zoom.toFixed(2))
  if (view.bounds) {
    params.set('w', view.bounds[0].toFixed(4))
    params.set('s', view.bounds[1].toFixed(4))
    params.set('e', view.bounds[2].toFixed(4))
    params.set('n', view.bounds[3].toFixed(4))
  }
  if (routeId) params.set('route', routeId)
  if (viewportOnly) params.set('inview', '1')
  const query = params.toString()
  const url = `${window.location.pathname}${query ? `?${query}` : ''}`
  window.history[mode === 'push' ? 'pushState' : 'replaceState'](
    { filters, view, routeId },
    '',
    url,
  )
}

function useRouteList(
  filters: Filters,
  view: ViewState,
  viewportOnly: boolean,
  retryToken: number,
) {
  const [state, setState] = useState<{
    routes: Route[]
    count: number
    loading: boolean
    error: string | null
  }>({ routes: [], count: 0, loading: true, error: null })
  const query = useMemo(() => {
    const params = new URLSearchParams({ page_size: '100' })
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value)
    })
    if (viewportOnly && view.bounds) {
      params.set('west', String(view.bounds[0]))
      params.set('south', String(view.bounds[1]))
      params.set('east', String(view.bounds[2]))
      params.set('north', String(view.bounds[3]))
    }
    return params.toString()
  }, [filters, view.bounds, viewportOnly])
  useEffect(() => {
    let active = true
    setState((current) => ({ ...current, loading: true, error: null }))
    apiClient
      .GET('/api/v1/routes/', {
        params: { query: Object.fromEntries(new URLSearchParams(query)) } as never,
      })
      .then(({ data, error }) => {
        if (!active) return
        if (error || !data) throw new Error('Route catalogue is unavailable')
        setState({ routes: data.results, count: data.count, loading: false, error: null })
      })
      .catch((error: unknown) => {
        if (active)
          setState((current) => ({
            ...current,
            loading: false,
            error: error instanceof Error ? error.message : 'Route catalogue is unavailable',
          }))
      })
    return () => {
      active = false
    }
  }, [query, retryToken])
  return state
}

function useViewport(view: ViewState, filters: Filters, ready: boolean, retryToken: number) {
  const [state, setState] = useState<{
    data: ViewportResponse | null
    loading: boolean
    error: string | null
  }>({ data: null, loading: false, error: null })
  useEffect(() => {
    if (!ready) return
    let active = true
    const span = Math.max(0.05, 35 / 2 ** view.zoom)
    const [west, south, east, north] = view.bounds ?? [
      view.longitude - span * 1.4,
      view.latitude - span,
      view.longitude + span * 1.4,
      view.latitude + span,
    ]
    const query = {
      west,
      south,
      east,
      north,
      zoom: Math.round(view.zoom),
      limit: 500,
      cell_limit: 10000,
      ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
    }
    setState((current) => ({ ...current, loading: true, error: null }))
    apiClient
      .GET('/api/v1/routes/viewport/', { params: { query } as never })
      .then(({ data, error }) => {
        if (!active) return
        if (error || !data) throw new Error('The map data could not be loaded')
        setState({ data, loading: false, error: null })
      })
      .catch((error: unknown) => {
        if (active)
          setState((current) => ({
            ...current,
            loading: false,
            error: error instanceof Error ? error.message : 'The map data could not be loaded',
          }))
      })
    return () => {
      active = false
    }
  }, [filters, ready, retryToken, view.bounds, view.latitude, view.longitude, view.zoom])
  return state
}

function App() {
  const initial = useMemo(parseState, [])
  const [filters, setFilters] = useState<Filters>(initial.filters)
  const [view, setView] = useState<ViewState>(initial.view)
  const [selectedId, setSelectedId] = useState<string | null>(initial.routeId)
  const [selected, setSelected] = useState<SpatialRoute | null>(null)
  const [selectedRecord, setSelectedRecord] = useState<Route | null>(null)
  const [selectedMetadataError, setSelectedMetadataError] = useState<string | null>(null)
  const [selectedGeometryError, setSelectedGeometryError] = useState<string | null>(null)
  const [selectedMetadataLoading, setSelectedMetadataLoading] = useState(false)
  const [selectedGeometryLoading, setSelectedGeometryLoading] = useState(false)
  const [metadataRetryToken, setMetadataRetryToken] = useState(0)
  const [geometryRetryToken, setGeometryRetryToken] = useState(0)
  const [overlap, setOverlap] = useState<string[]>([])
  const [panelOpen, setPanelOpen] = useState(true)
  const [mapReady, setMapReady] = useState(false)
  const [viewportOnly, setViewportOnly] = useState(initial.viewportOnly)
  const [retryToken, setRetryToken] = useState(0)
  const [mapRetry, setMapRetry] = useState(0)
  const [mapError, setMapError] = useState<string | null>(null)
  const [mobilePanelHeight, setMobilePanelHeight] = useState<number | null>(null)
  const [mobileDetailHeight, setMobileDetailHeight] = useState<number | null>(null)
  const cameraSyncRef = useRef(false)
  const mapNode = useRef<HTMLDivElement>(null)
  const panelNode = useRef<HTMLElement>(null)
  const detailNode = useRef<HTMLElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const { routes, count, loading, error } = useRouteList(filters, view, viewportOnly, retryToken)
  const viewport = useViewport(view, filters, mapReady, retryToken)
  const selectedRoute = routes.find((route) => route.id === selectedId) ?? selectedRecord
  const mobileSelectionActive = Boolean(selectedId && window.innerWidth <= 700)
  const selectionVisible = Boolean(
    selectedId &&
    (selectedRoute ||
      selectedMetadataLoading ||
      selectedGeometryLoading ||
      selectedMetadataError ||
      selectedGeometryError),
  )
  const selectedIndex = overlap.indexOf(selectedId ?? '')
  const displayRoutes = routes
  const mapData = viewport.data
  const selectRoute = useCallback(
    (id: string | null, push = true) => {
      setSelectedId(id)
      setOverlap(id ? [id] : [])
      if (id && window.innerWidth <= 700) {
        setMobilePanelHeight(null)
        setPanelOpen(false)
      }
      if (push) writeUrl(filters, view, id, 'push', viewportOnly)
    },
    [filters, view, viewportOnly],
  )
  const filtersRef = useRef(filters)
  const viewRef = useRef(view)
  const viewportOnlyRef = useRef(viewportOnly)
  filtersRef.current = filters
  viewRef.current = view
  viewportOnlyRef.current = viewportOnly

  useEffect(() => {
    if (!selectedId) {
      setSelected(null)
      setSelectedRecord(null)
      setSelectedMetadataError(null)
      setSelectedGeometryError(null)
      setSelectedMetadataLoading(false)
      setSelectedGeometryLoading(false)
      return
    }
    setSelectedRecord(null)
    setSelectedMetadataError(null)
    setSelectedGeometryError(null)
    setSelectedMetadataLoading(true)
    setSelectedGeometryLoading(true)
  }, [selectedId])
  useEffect(() => {
    if (selectedId && window.innerWidth <= 700) {
      setMobilePanelHeight(null)
      setPanelOpen(false)
    }
  }, [selectedId])
  useEffect(() => {
    if (!selectedId) return
    let active = true
    setSelectedMetadataLoading(true)
    setSelectedMetadataError(null)
    apiClient
      .GET('/api/v1/routes/{route_id}/', { params: { path: { route_id: selectedId } } })
      .then(({ data, error }) => {
        if (!active) return
        if (error || !data) {
          setSelectedMetadataError('Route details are unavailable right now.')
        } else {
          setSelectedRecord(data)
          setSelectedMetadataError(null)
        }
        setSelectedMetadataLoading(false)
      })
      .catch(() => {
        if (active) {
          setSelectedMetadataError('Route details are unavailable right now.')
          setSelectedMetadataLoading(false)
        }
      })
    return () => {
      active = false
    }
  }, [metadataRetryToken, retryToken, selectedId])
  useEffect(() => {
    if (!selectedId) return
    let active = true
    setSelectedGeometryLoading(true)
    setSelectedGeometryError(null)
    apiClient
      .GET('/api/v1/routes/{route_id}/geometry/', { params: { path: { route_id: selectedId } } })
      .then(({ data, error }) => {
        if (active) {
          if (error || !data) {
            setSelected(null)
            setSelectedGeometryError('Full route geometry is unavailable right now.')
          } else {
            setSelected(data)
            setSelectedGeometryError(null)
          }
          setSelectedGeometryLoading(false)
        }
      })
      .catch(() => {
        if (active) {
          setSelected(null)
          setSelectedGeometryError('Full route geometry is unavailable right now.')
          setSelectedGeometryLoading(false)
        }
      })
    return () => {
      active = false
    }
  }, [geometryRetryToken, retryToken, selectedId])
  useEffect(() => {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ filters, routeId: selectedId, viewportOnly }),
      )
    } catch {
      /* private browsing */
    }
    writeUrl(filters, view, selectedId, 'replace', viewportOnly)
  }, [filters, view, selectedId, viewportOnly])
  useEffect(() => {
    const handlePopState = () => {
      const next = parseState()
      setFilters(next.filters)
      setView(next.view)
      setSelectedId(next.routeId)
      setOverlap(next.routeId ? [next.routeId] : [])
      setViewportOnly(next.viewportOnly)
      const map = mapRef.current
      if (map) {
        cameraSyncRef.current = true
        try {
          map.jumpTo({ center: [next.view.longitude, next.view.latitude], zoom: next.view.zoom })
        } catch {
          cameraSyncRef.current = false
        }
        window.setTimeout(() => {
          cameraSyncRef.current = false
        }, 0)
      }
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  useEffect(() => {
    if (!mapNode.current) return
    setMapError(null)
    let map: MapLibreMap | null = null
    let disposed = false
    import('maplibre-gl')
      .then(({ default: maplibregl }) => {
        if (disposed || !mapNode.current) return
        let mapInstance: MapLibreMap
        try {
          mapInstance = new maplibregl.Map({
            container: mapNode.current,
            style: MAP_PROVIDER.style,
            center: [view.longitude, view.latitude],
            zoom: view.zoom,
            attributionControl: false,
            keyboard: true,
          })
        } catch {
          setMapError('The interactive map could not start.')
          return
        }
        map = mapInstance
        mapRef.current = mapInstance
        mapInstance.addControl(
          new maplibregl.AttributionControl({ customAttribution: MAP_PROVIDER.attribution }),
          'bottom-left',
        )
        mapInstance.addControl(
          new maplibregl.NavigationControl({ showCompass: false }),
          'bottom-right',
        )
        mapInstance.on('load', () => {
          mapInstance.addSource(HEAT_SOURCE, {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] },
          })
          mapInstance.addLayer({
            id: 'heat-cells',
            type: 'fill',
            source: HEAT_SOURCE,
            paint: {
              'fill-color': [
                'interpolate',
                ['linear'],
                ['get', 'count'],
                1,
                '#a6c8bd',
                4,
                '#efb84a',
                10,
                '#c95135',
              ],
              'fill-opacity': 0.55,
              'fill-outline-color': '#fff3d7',
            },
          })
          mapInstance.addSource(ROUTE_SOURCE, {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] },
          })
          mapInstance.addLayer({
            id: 'browse-routes',
            type: 'line',
            source: ROUTE_SOURCE,
            paint: {
              'line-color': '#315d52',
              'line-width': ['interpolate', ['linear'], ['zoom'], 7, 1.8, 14, 3.4],
              'line-opacity': 0.72,
            },
          })
          mapInstance.addSource(SELECTED_SOURCE, {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] },
          })
          mapInstance.addLayer({
            id: 'selected-route-halo',
            type: 'line',
            source: SELECTED_SOURCE,
            paint: { 'line-color': '#fff8e6', 'line-width': 9, 'line-opacity': 0.94 },
          })
          mapInstance.addLayer({
            id: 'selected-route-line',
            type: 'line',
            source: SELECTED_SOURCE,
            paint: { 'line-color': '#d34d32', 'line-width': 5, 'line-opacity': 1 },
          })
          setMapReady(true)
        })
        mapInstance.on('error', (event) => {
          if (event.error) setMapError('Map tiles are unavailable. You can retry the map.')
        })
        const syncView = () => {
          if (cameraSyncRef.current) {
            cameraSyncRef.current = false
            return
          }
          const center = mapInstance.getCenter()
          const bounds = mapInstance.getBounds()
          const nextView: ViewState = {
            longitude: center.lng,
            latitude: center.lat,
            zoom: mapInstance.getZoom(),
            bounds: [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()],
          }
          const previousView = viewRef.current
          if (
            previousView.longitude === nextView.longitude &&
            previousView.latitude === nextView.latitude &&
            previousView.zoom === nextView.zoom &&
            JSON.stringify(previousView.bounds) === JSON.stringify(nextView.bounds)
          )
            return
          setView(nextView)
        }
        mapInstance.on('load', syncView)
        mapInstance.on('moveend', syncView)
        mapInstance.on('click', (event) => {
          if (!mapInstance.getLayer('browse-routes')) return
          const features = mapInstance.queryRenderedFeatures(event.point, {
            layers: ['browse-routes'],
          })
          const ids = [
            ...new Set(features.map((feature) => String(feature.properties?.routeId ?? ''))),
          ].filter(Boolean)
          if (ids.length) {
            setOverlap(ids)
            setSelectedId(ids[0])
            writeUrl(filtersRef.current, viewRef.current, ids[0], 'push', viewportOnlyRef.current)
          }
        })
      })
      .catch(() => setMapError('The interactive map could not start.'))
    return () => {
      disposed = true
      setMapReady(false)
      map?.remove()
      mapRef.current = null
    }
    // The map is intentionally created once; state updates its sources below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapRetry])
  useEffect(() => {
    const panel = panelNode.current
    if (!panel) return
    const updateHeight = () => {
      const height = panel.getBoundingClientRect().height
      setMobilePanelHeight(height > 0 ? height : null)
    }
    updateHeight()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(updateHeight)
    observer.observe(panel)
    return () => observer.disconnect()
  }, [panelOpen])
  useEffect(() => {
    const detail = detailNode.current
    if (!detail) {
      setMobileDetailHeight(null)
      return
    }
    const updateHeight = () => {
      const height = detail.getBoundingClientRect().height
      setMobileDetailHeight(height > 0 ? height : null)
    }
    updateHeight()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(updateHeight)
    observer.observe(detail)
    return () => observer.disconnect()
  }, [
    selectedGeometryError,
    selectedGeometryLoading,
    selectedMetadataError,
    selectedMetadataLoading,
    selectedRoute,
    selectionVisible,
  ])
  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady || !mapData) return
    const data = mapData
    const heatSource = map.getSource(HEAT_SOURCE) as GeoJSONSource | undefined
    const routeSource = map.getSource(ROUTE_SOURCE) as GeoJSONSource | undefined
    heatSource?.setData({
      type: 'FeatureCollection',
      features: data.cells
        .filter((cell) => cell.geometry)
        .map((cell) => ({
          type: 'Feature',
          properties: { count: cell.count },
          geometry: cell.geometry!,
        })),
    })
    routeSource?.setData({
      type: 'FeatureCollection',
      features: data.routes
        .filter((route) => route.geometry)
        .map((route) => ({
          type: 'Feature',
          properties: { routeId: route.id, title: route.title },
          geometry: route.geometry!,
        })),
    })
    if (map.getLayer('heat-cells'))
      map.setLayoutProperty(
        'heat-cells',
        'visibility',
        data.mode === 'heatmap' ? 'visible' : 'none',
      )
    if (map.getLayer('browse-routes'))
      map.setLayoutProperty(
        'browse-routes',
        'visibility',
        data.mode === 'routes' ? 'visible' : 'none',
      )
  }, [mapData, mapReady])
  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady) return
    const source = map.getSource(SELECTED_SOURCE) as GeoJSONSource | undefined
    source?.setData({
      type: 'FeatureCollection',
      features: selected?.geometry
        ? [{ type: 'Feature', properties: {}, geometry: selected.geometry }]
        : [],
    })
    if (selected?.geometry) {
      const bounds = geometryBounds(selected.geometry)
      if (bounds) {
        const mobile = window.innerWidth <= 700
        const mobileSheetHeight = mobilePanelHeight ?? (panelOpen ? window.innerHeight * 0.67 : 42)
        const mobileDetailOffset = mobileDetailHeight ?? 150
        map.fitBounds(bounds, {
          padding: mobile
            ? {
                top: 80,
                right: 24,
                bottom: mobileSheetHeight + mobileDetailOffset + 24,
                left: 24,
              }
            : { top: 120, right: panelOpen ? 430 : 90, bottom: 100, left: 90 },
          maxZoom: 13,
          duration: 600,
        })
      }
    }
  }, [mapReady, mobileDetailHeight, mobilePanelHeight, panelOpen, selected])

  const updateFilter = (key: keyof Filters, value: string) =>
    setFilters((current) => ({ ...current, [key]: value }))
  const resetFilters = () => setFilters(DEFAULT_FILTERS)
  const retry = () => setRetryToken((current) => current + 1)
  const nextOverlap = (direction: number) => {
    if (overlap.length < 2) return
    const nextIndex = (Math.max(0, selectedIndex) + direction + overlap.length) % overlap.length
    setSelectedId(overlap[nextIndex])
    writeUrl(filters, view, overlap[nextIndex], 'push', viewportOnly)
  }
  return (
    <main
      className="app-shell"
      style={
        {
          '--mobile-sheet-height': panelOpen ? '67vh' : '42px',
          ...(mobilePanelHeight ? { '--mobile-sheet-height': `${mobilePanelHeight}px` } : {}),
        } as CSSProperties
      }
    >
      <header className="topbar">
        <a className="wordmark" href="/" aria-label="BikeMapy home">
          <span className="wordmark-mark" aria-hidden="true">
            ↗
          </span>
          <span>BikeMapy</span>
        </a>
        <div className="topbar-meta">
          <span className="live-indicator">
            <i /> {count ? `${count} rides indexed` : 'Open ride archive'}
          </span>
          <button type="button" className="language-switcher" aria-label="Change language">
            EN / CZ
          </button>
        </div>
      </header>
      <section className="map-stage" aria-label="Route map">
        <div ref={mapNode} className="map-canvas" aria-label="Interactive route map" />
        <div className="map-vignette" aria-hidden="true" />
        <div className="map-status" role="status">
          {viewport.loading
            ? 'Updating this view…'
            : viewport.error
              ? viewport.error
              : Object.values(filters).some(Boolean) && viewport.data?.mode === 'heatmap'
                ? 'Filtered route density'
                : viewport.data?.mode === 'heatmap'
                  ? 'Route density'
                  : `${viewport.data?.routes.length ?? 0} routes in view`}
        </div>
        <div className="map-empty">
          {(mapError || viewport.error) && (
            <>
              <strong>{mapError ?? 'Map data paused'}</strong>
              <button
                type="button"
                onClick={mapError ? () => setMapRetry((current) => current + 1) : retry}
              >
                {mapError ? 'Retry map' : 'Try again'}
              </button>
            </>
          )}
        </div>
        <div className="map-legend">
          <span className="legend-line" /> selected <span className="legend-muted" /> nearby
        </div>
      </section>
      <aside
        ref={panelNode}
        className={`route-panel ${panelOpen ? 'is-open' : 'is-collapsed'}`}
        aria-label="Route search"
      >
        <button
          type="button"
          className="panel-toggle"
          onClick={() => {
            if (mobileSelectionActive) return
            setMobilePanelHeight(null)
            setPanelOpen((open) => !open)
          }}
          disabled={mobileSelectionActive}
          aria-expanded={panelOpen}
          aria-controls="route-browser"
        >
          {panelOpen ? 'Hide routes' : 'Show routes'}{' '}
          <span aria-hidden="true">{panelOpen ? '−' : '+'}</span>
        </button>
        <div id="route-browser" className="panel-content">
          <p className="eyebrow">A map with a memory</p>
          <h1>
            Find the ride
            <br />
            worth repeating.
          </h1>
          <p className="intro">
            Explore the roads people return to. Search the archive, then let the map take the lead.
          </p>
          <label className="search-field">
            <span aria-hidden="true">⌕</span>
            <span className="sr-only">Search routes</span>
            <input
              type="search"
              value={filters.search}
              onChange={(event) => updateFilter('search', event.target.value)}
              placeholder="Search places, authors, routes"
            />
          </label>
          <div className="filter-grid">
            <label>
              Author
              <input
                value={filters.author}
                onChange={(event) => updateFilter('author', event.target.value)}
                placeholder="e.g. Jana"
              />
            </label>
            <label>
              Category
              <input
                value={filters.category}
                onChange={(event) => updateFilter('category', event.target.value)}
                placeholder="e.g. gravel"
              />
            </label>
            <label>
              Distance from (m)
              <input
                inputMode="numeric"
                value={filters.min_distance_m}
                onChange={(event) => updateFilter('min_distance_m', event.target.value)}
              />
            </label>
            <label>
              Distance to (m)
              <input
                inputMode="numeric"
                value={filters.max_distance_m}
                onChange={(event) => updateFilter('max_distance_m', event.target.value)}
              />
            </label>
            <label>
              Climb from (m)
              <input
                inputMode="numeric"
                value={filters.min_ascent_m}
                onChange={(event) => updateFilter('min_ascent_m', event.target.value)}
              />
            </label>
            <label>
              Climb to (m)
              <input
                inputMode="numeric"
                value={filters.max_ascent_m}
                onChange={(event) => updateFilter('max_ascent_m', event.target.value)}
              />
            </label>
          </div>
          <div className="results-heading">
            <span>
              {loading
                ? 'Reading the archive…'
                : error
                  ? 'Archive unavailable'
                  : `${displayRoutes.length} rides found`}
            </span>
            <button
              type="button"
              onClick={resetFilters}
              disabled={!Object.values(filters).some(Boolean)}
            >
              Clear filters
            </button>
          </div>
          <label className="viewport-filter">
            <input
              type="checkbox"
              checked={viewportOnly}
              onChange={(event) => setViewportOnly(event.target.checked)}
            />
            <span>Current viewport</span>
            {viewportOnly && viewport.data?.mode === 'heatmap' && (
              <small>List follows the current map bounds</small>
            )}
          </label>
          {error && (
            <div className="notice error-notice">
              <strong>We lost the archive connection.</strong>
              <span>Check the API and try again.</span>
              <button type="button" onClick={retry}>
                Retry
              </button>
            </div>
          )}
          {!loading && !error && displayRoutes.length === 0 && (
            <div className="notice">
              <strong>No rides match yet.</strong>
              <span>Try a wider search or clear the filters.</span>
              <button type="button" onClick={resetFilters}>
                Clear filters
              </button>
            </div>
          )}
          <div className="route-list" aria-label="Routes">
            {displayRoutes.map((route) => (
              <button
                key={route.id}
                type="button"
                className={`route-card ${route.id === selectedId ? 'is-selected' : ''}`}
                onClick={() => selectRoute(route.id)}
              >
                <span className="route-card-title">{route.title}</span>
                <span className="route-card-meta">
                  <span>{route.categories[0]?.name ?? 'Uncategorised'}</span>
                  <span>
                    {route.distance_m
                      ? `${(Number(route.distance_m) / 1000).toFixed(1)} km`
                      : 'Distance unknown'}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </div>
      </aside>
      {selectionVisible && (
        <section ref={detailNode} className="route-detail" aria-label="Selected route details">
          <div className="detail-kicker">
            Selected route{' '}
            {overlap.length > 1 && (
              <span className="overlap-picker">
                <button
                  type="button"
                  onClick={() => nextOverlap(-1)}
                  aria-label="Previous overlapping route"
                >
                  ‹
                </button>
                <span>
                  {Math.max(1, selectedIndex + 1)} / {overlap.length}
                </span>
                <button
                  type="button"
                  onClick={() => nextOverlap(1)}
                  aria-label="Next overlapping route"
                >
                  ›
                </button>
              </span>
            )}
          </div>
          <h2>
            {selectedRoute?.title ??
              (selectedMetadataLoading ? 'Loading route…' : 'Route details unavailable')}
          </h2>
          <div className="detail-stats">
            <span>
              <b>
                {selectedRoute?.distance_m
                  ? `${(Number(selectedRoute.distance_m) / 1000).toFixed(1)}`
                  : '—'}
              </b>{' '}
              km
            </span>
            <span>
              <b>{selectedRoute?.ascent_m ? Math.round(Number(selectedRoute.ascent_m)) : '—'}</b> m
              climb
            </span>
            <span>
              <b>{selectedRoute?.categories[0]?.name ?? 'Route'}</b> category
            </span>
          </div>
          <p className="detail-source">
            {selectedMetadataError ??
              selectedGeometryError ??
              (selectedRoute
                ? `${selectedRoute.source_status === 'verified' ? 'Verified source' : 'Community source'} · ${selectedGeometryLoading ? 'Loading full geometry…' : 'Full geometry framed on map'}`
                : 'Route details are still loading…')}
          </p>
          {selectedMetadataError && (
            <button
              type="button"
              className="detail-retry"
              onClick={() => setMetadataRetryToken((current) => current + 1)}
            >
              Retry route details
            </button>
          )}
          {selectedGeometryError && (
            <button
              type="button"
              className="detail-retry"
              onClick={() => setGeometryRetryToken((current) => current + 1)}
            >
              Retry geometry
            </button>
          )}
          <button
            type="button"
            className="close-detail"
            onClick={() => selectRoute(null)}
            aria-label="Close route details"
          >
            ×
          </button>
        </section>
      )}
    </main>
  )
}

export default App
