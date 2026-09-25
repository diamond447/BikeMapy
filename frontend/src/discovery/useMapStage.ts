import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import type { GeoJSONSource, Map as MapLibreMap } from 'maplibre-gl'
import { MAP_PROVIDER } from '../mapProvider'
import { geometryBounds } from './geometry'
import type { Geometry, ViewState, SpatialRoute, ViewportResponse } from './types'
import type { Copy } from '../i18n/types'

const ROUTE_SOURCE = 'browse-routes'
const HEAT_SOURCE = 'browse-heatmap'
const SELECTED_SOURCE = 'selected-route'

type GeoJSONSourceData = Exclude<Parameters<GeoJSONSource['setData']>[0], string>
type GeoJSONFeature = Extract<GeoJSONSourceData, { type: 'FeatureCollection' }>['features'][number]
type GeoJSONGeometry = NonNullable<GeoJSONFeature['geometry']>

const asGeoJSONGeometry = (geometry: Geometry): GeoJSONGeometry =>
  geometry as unknown as GeoJSONGeometry

type MapStageOptions = {
  mapNode: RefObject<HTMLDivElement | null>
  view: ViewState
  mapData: ViewportResponse | null
  selected: SpatialRoute | null
  selectedId: string | null
  hoveredRouteId: string | null
  panelOpen: boolean
  mobilePanelHeight: number | null
  mobileDetailHeight: number | null
  mapRetry: number
  copy: Pick<Copy, 'mapStartError' | 'mapTilesError'>
  onViewChange: (view: ViewState) => void
  onRouteClick: (ids: string[]) => void
  onHover: (id: string | null) => void
  onReady: (ready: boolean) => void
}

export function useMapStage({
  mapNode,
  view,
  mapData,
  selected,
  selectedId,
  hoveredRouteId,
  panelOpen,
  mobilePanelHeight,
  mobileDetailHeight,
  mapRetry,
  copy,
  onViewChange,
  onRouteClick,
  onHover,
  onReady,
}: MapStageOptions) {
  const [mapReady, setMapReady] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const cameraSyncRef = useRef(false)
  const copyRef = useRef(copy)
  const viewRef = useRef(view)
  useEffect(() => {
    copyRef.current = copy
    viewRef.current = view
  }, [copy, view])

  const jumpTo = useCallback((nextView: ViewState) => {
    const map = mapRef.current
    if (!map) return
    cameraSyncRef.current = true
    try {
      map.jumpTo({ center: [nextView.longitude, nextView.latitude], zoom: nextView.zoom })
    } catch {
      cameraSyncRef.current = false
    }
    window.setTimeout(() => {
      cameraSyncRef.current = false
    }, 0)
  }, [])

  useEffect(() => {
    const mapElement = mapNode.current
    if (!mapElement) return
    // Reset the map error whenever the map is explicitly retried.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setError(null)
    let map: MapLibreMap | null = null
    let mapResizeObserver: ResizeObserver | undefined
    let disposed = false
    import('maplibre-gl')
      .then(async (maplibregl) => {
        if (disposed || !mapNode.current) return
        const { default: workerUrl } =
          await import('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url')
        if (disposed || !mapNode.current) return
        maplibregl.setWorkerUrl(workerUrl)
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
          setError(copyRef.current.mapStartError)
          return
        }
        map = mapInstance
        mapRef.current = mapInstance
        if (typeof ResizeObserver !== 'undefined') {
          mapResizeObserver = new ResizeObserver(() => mapInstance.resize())
          mapResizeObserver.observe(mapElement)
        }
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
          mapInstance.addLayer({
            id: 'hovered-route',
            type: 'line',
            source: ROUTE_SOURCE,
            filter: ['==', ['get', 'routeId'], ''],
            paint: {
              'line-color': '#e4d866',
              'line-width': ['interpolate', ['linear'], ['zoom'], 7, 4, 14, 7],
              'line-opacity': 0.9,
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
            paint: {
              'line-color': '#d34d32',
              'line-width': 5,
              'line-opacity': 1,
              'line-dasharray': [1.2, 0.8],
            },
          })
          mapNode.current?.setAttribute('data-map-ready', 'true')
          mapNode.current?.setAttribute(
            'data-map-sources',
            'browse-heatmap,browse-routes,selected-route',
          )
          setMapReady(true)
          onReady(true)
        })
        mapInstance.on('error', (event) => {
          if (event.error) setError(copyRef.current.mapTilesError)
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
          onViewChange(nextView)
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
            onRouteClick(ids)
          }
        })
        mapInstance.on('mousemove', (event) => {
          if (!mapInstance.getLayer('browse-routes')) return
          const feature = mapInstance.queryRenderedFeatures(event.point, {
            layers: ['browse-routes'],
          })[0]
          const id = String(feature?.properties?.routeId ?? '')
          onHover(id || null)
        })
        mapInstance.on('mouseout', () => onHover(null))
      })
      .catch(() => setError(copyRef.current.mapStartError))
    return () => {
      disposed = true
      setMapReady(false)
      onReady(false)
      mapElement.setAttribute('data-map-ready', 'false')
      mapElement.removeAttribute('data-map-route-features')
      mapElement.removeAttribute('data-map-rendered-features')
      map?.remove()
      mapResizeObserver?.disconnect()
      mapRef.current = null
    }
    // The map is intentionally created once; state updates its sources below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapRetry])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady || !mapData) return
    const data = mapData
    const heatSource = map.getSource(HEAT_SOURCE) as GeoJSONSource | undefined
    const routeSource = map.getSource(ROUTE_SOURCE) as GeoJSONSource | undefined
    const routeFeatures = data.routes.filter((route) => route.geometry)
    heatSource?.setData({
      type: 'FeatureCollection',
      features: data.cells
        .filter((cell) => cell.geometry)
        .map((cell) => ({
          type: 'Feature',
          properties: { count: cell.count },
          geometry: asGeoJSONGeometry(cell.geometry!),
        })),
    })
    routeSource?.setData({
      type: 'FeatureCollection',
      features: routeFeatures.map((route) => ({
        type: 'Feature',
        properties: {
          routeId: route.id,
          title: route.title,
          selected: route.id === selectedId,
          hovered: route.id === hoveredRouteId,
        },
        geometry: asGeoJSONGeometry(route.geometry!),
      })),
    })
    mapNode.current?.setAttribute('data-map-route-features', String(routeFeatures.length))
    map.once('idle', () => {
      const renderedFeatureCount = map.queryRenderedFeatures({ layers: ['browse-routes'] }).length
      mapNode.current?.setAttribute('data-map-rendered-features', String(renderedFeatureCount))
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
    if (map.getLayer('hovered-route') && typeof map.setFilter === 'function')
      map.setFilter('hovered-route', ['==', ['get', 'routeId'], hoveredRouteId ?? ''])
    // mapNode is a stable DOM ref owned by the composition layer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hoveredRouteId, mapData, mapReady, selectedId])
  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady) return
    const source = map.getSource(SELECTED_SOURCE) as GeoJSONSource | undefined
    source?.setData({
      type: 'FeatureCollection',
      features: selected?.geometry
        ? [{ type: 'Feature', properties: {}, geometry: asGeoJSONGeometry(selected.geometry) }]
        : [],
    })
    if (selected?.geometry) {
      const bounds = geometryBounds(selected.geometry)
      if (bounds) {
        const mobile = window.innerWidth <= 700
        const reducedMotion =
          window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
        const mobileSheetHeight = mobilePanelHeight ?? (panelOpen ? window.innerHeight * 0.67 : 42)
        const mobileDetailOffset = mobileDetailHeight ?? 150
        const padding = mobile
          ? {
              top: 80,
              right: 24,
              bottom: mobileSheetHeight + mobileDetailOffset + 24,
              left: 24,
            }
          : { top: 120, right: 90, bottom: 100, left: 90 }
        const fitCenter = () => {
          if (typeof map.project !== 'function' || typeof map.getContainer !== 'function') return
          const point = map.project([
            (bounds[0][0] + bounds[1][0]) / 2,
            (bounds[0][1] + bounds[1][1]) / 2,
          ])
          const container = map.getContainer().getBoundingClientRect()
          mapNode.current?.setAttribute(
            'data-map-fit-center',
            JSON.stringify({
              x: point.x,
              y: point.y,
              width: container.width,
              height: container.height,
            }),
          )
        }
        map.fitBounds(bounds, {
          padding,
          maxZoom: 13,
          duration: reducedMotion ? 0 : 600,
        })
        if (reducedMotion) fitCenter()
        else map.once('moveend', fitCenter)
      }
    }
    // mapNode is a stable DOM ref owned by the composition layer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapReady, mobileDetailHeight, mobilePanelHeight, panelOpen, selected])

  return { mapRef, mapError: error, jumpTo }
}
