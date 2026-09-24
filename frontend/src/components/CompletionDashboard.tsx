/* eslint-disable react-refresh/only-export-components -- map helpers are unit-tested. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AttributionControl,
  Map as MapLibreMap,
  NavigationControl,
  setWorkerUrl,
} from 'maplibre-gl'
import type { GeoJSONSource } from 'maplibre-gl'

import { apiClient, rememberCsrfToken } from '../api/client'
import type { components } from '../api/generated/schema'
import { setMapLibreWorker } from '../maplibreWorker'
import { MAP_PROVIDER } from '../mapProvider'
import type { Copy } from '../i18n/types'

type RouteSummary = components['schemas']['ReferenceRouteList'] & {
  source_kind?: string
  stages?: Array<{ id: string; title: string; route_number?: string; geometry?: unknown }>
}
type Projection = {
  status: string
  total_length_meters: string
  covered_length_meters: string
  completion_percent: string
  error?: string
  covered_geometry?: unknown
  monthly?: Array<{ month: string; covered_length_meters: string }>
  partial?: boolean
}
type RouteCompletion = {
  route_id: string
  title: string
  route_number?: string
  source_kind?: string
  geometry?: unknown
  attribution?: Record<string, unknown>
  player: Projection
  competition: Projection | null
  stages: Array<{
    route_id: string
    title: string
    route_number?: string
    geometry?: unknown
    player: Projection
    competition: Projection | null
  }>
}

const STATE_KEY = 'bikemapy:game-completion'
const MAX_ROUTE_PAGES = 10
const ROUTES_PAGE_SIZE = 100

function readState(): { mode?: 'player' | 'competition'; routeId?: string; stageId?: string } {
  try {
    const value = JSON.parse(sessionStorage.getItem(STATE_KEY) ?? '{}') as Record<string, unknown>
    return {
      mode: value.mode === 'competition' ? 'competition' : 'player',
      routeId: typeof value.routeId === 'string' ? value.routeId : undefined,
      stageId: typeof value.stageId === 'string' ? value.stageId : undefined,
    }
  } catch {
    return {}
  }
}

function saveState(state: { mode: 'player' | 'competition'; routeId?: string; stageId?: string }) {
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify(state))
  } catch {
    /* Private browsing can disable session storage. */
  }
}

function detailKey(competitionId: string | undefined, routeId: string) {
  return `${competitionId ?? ''}:${routeId}`
}

function currentPragueMonth() {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: 'Europe/Prague',
    year: 'numeric',
    month: '2-digit',
  }).formatToParts()
  const year = parts.find((part) => part.type === 'year')?.value ?? ''
  const month = parts.find((part) => part.type === 'month')?.value ?? ''
  return `${year}-${month}`
}

function nextCursor(value: unknown) {
  if (typeof value !== 'string' || !value) return undefined
  try {
    return new URL(value, window.location.origin).searchParams.get('cursor') ?? undefined
  } catch {
    return undefined
  }
}

export function completionFeature(geometry: unknown, properties: Record<string, string> = {}) {
  return { type: 'Feature' as const, properties, geometry: geometry as never }
}

function coordinates(geometry: unknown, result: Array<[number, number]> = []) {
  if (!geometry || typeof geometry !== 'object') return result
  const value = geometry as { coordinates?: unknown; geometry?: unknown }
  if ('geometry' in value) return coordinates(value.geometry, result)
  if (!Array.isArray(value.coordinates)) return result
  const walk = (part: unknown): void => {
    if (Array.isArray(part) && part.length >= 2 && part.every((item) => typeof item === 'number')) {
      result.push([part[0] as number, part[1] as number])
      return
    }
    if (Array.isArray(part)) part.forEach(walk)
  }
  walk(value.coordinates)
  return result
}

export function completionPercent(projection: Projection | null | undefined) {
  return projection ? `${Number(projection.completion_percent).toFixed(1)}%` : '—'
}

function statusLabel(status: string, copy: Copy) {
  if (status === 'pending') return copy.gameCompletionPending
  if (status === 'stale') return copy.gameCompletionStale
  if (status === 'failed') return copy.gameCompletionFailed
  return ''
}

export function CompletionDashboard({
  copy,
  competitions,
  competitionId,
  setCompetitionId,
  signedOut,
}: {
  copy: Copy
  competitions: components['schemas']['Competition'][]
  competitionId?: string
  setCompetitionId: (id: string) => void
  signedOut: boolean
}) {
  const initial = useMemo(() => readState(), [])
  const [mode, setMode] = useState<'player' | 'competition'>(initial.mode ?? 'player')
  const [routes, setRoutes] = useState<RouteSummary[]>([])
  const [details, setDetails] = useState<Record<string, RouteCompletion>>({})
  const [routeId, setRouteId] = useState<string | undefined>(initial.routeId)
  const [stageId, setStageId] = useState<string | undefined>(initial.stageId)
  const [loading, setLoading] = useState(true)
  const [catalogueIncomplete, setCatalogueIncomplete] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const mapNode = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const mapLoaded = useRef(false)
  const framedKey = useRef<string | null>(null)
  const routeRequestSequence = useRef(0)
  const detailRequestSequence = useRef(0)
  const [mapReady, setMapReady] = useState(false)
  const currentMonth = useMemo(() => currentPragueMonth(), [])

  const loadRoutes = useCallback(async () => {
    if (!competitionId) {
      setRoutes([])
      setCatalogueIncomplete(false)
      setLoading(false)
      return
    }
    const requestSequence = ++routeRequestSequence.current
    setLoading(true)
    setError(null)
    setDetailError(null)
    setCatalogueIncomplete(false)
    try {
      const records: RouteSummary[] = []
      let cursor: string | undefined
      let incomplete = false
      for (let page = 0; page < MAX_ROUTE_PAGES; page += 1) {
        const result = await apiClient.GET('/api/v1/game/reference-routes/', {
          params: {
            query: { page_size: ROUTES_PAGE_SIZE, competition_id: competitionId, cursor },
          },
          credentials: 'include',
        })
        rememberCsrfToken(result.response)
        if (result.response?.status === 401) return
        if (requestSequence !== routeRequestSequence.current) return
        if (!result.data) throw new Error('routes')
        records.push(...((result.data.results ?? []) as RouteSummary[]))
        cursor = nextCursor(result.data.next)
        if (!cursor) break
        if (page === MAX_ROUTE_PAGES - 1) incomplete = true
      }
      if (requestSequence !== routeRequestSequence.current) return
      setRoutes(records)
      setCatalogueIncomplete(incomplete)
      setRouteId((current) => {
        const candidate = records.find((route) => route.id === current) ?? records[0]
        return candidate?.id
      })
    } catch {
      if (requestSequence === routeRequestSequence.current) setError(copy.gameCompletionError)
    } finally {
      if (requestSequence === routeRequestSequence.current) setLoading(false)
    }
  }, [competitionId, copy.gameCompletionError])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadRoutes(), 0)
    return () => window.clearTimeout(timer)
  }, [competitionId, loadRoutes])

  const loadDetail = useCallback(
    async (id: string) => {
      if (!competitionId) return
      const requestSequence = ++detailRequestSequence.current
      const key = detailKey(competitionId, id)
      setDetailLoading(true)
      setError(null)
      setDetailError(null)
      try {
        const result = await apiClient.GET('/api/v1/game/reference-routes/{route_id}/completion/', {
          params: { path: { route_id: id }, query: { competition_id: competitionId } },
          credentials: 'include',
        })
        if (requestSequence !== detailRequestSequence.current) return
        if (!result.data) throw new Error('completion')
        setDetails((current) => ({ ...current, [key]: result.data as unknown as RouteCompletion }))
      } catch {
        if (requestSequence !== detailRequestSequence.current) return
        setDetailError(copy.gameCompletionError)
        setError(copy.gameCompletionError)
      } finally {
        if (requestSequence === detailRequestSequence.current) setDetailLoading(false)
      }
    },
    [competitionId, copy.gameCompletionError],
  )

  useEffect(() => {
    if (!routeId || !competitionId || details[detailKey(competitionId, routeId)]) return
    const timer = window.setTimeout(() => void loadDetail(routeId), 0)
    return () => window.clearTimeout(timer)
  }, [competitionId, details, loadDetail, routeId])

  useEffect(() => {
    framedKey.current = null
  }, [competitionId])

  useEffect(() => {
    saveState({ mode, routeId, stageId })
  }, [mode, routeId, stageId])

  useEffect(() => {
    if (!mapNode.current || map.current) return
    setMapLibreWorker(setWorkerUrl)
    const instance = new MapLibreMap({
      container: mapNode.current,
      style: MAP_PROVIDER.style,
      center: [16.6, 49.2],
      zoom: 7.3,
      attributionControl: false,
      keyboard: true,
    })
    map.current = instance
    instance.addControl(new NavigationControl({ showCompass: true }), 'top-right')
    instance.addControl(new AttributionControl({ customAttribution: MAP_PROVIDER.attribution }))
    instance.on('load', () => {
      instance.addSource('reference-route', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      instance.addSource('reference-covered', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      instance.addLayer({
        id: 'reference-route-line',
        type: 'line',
        source: 'reference-route',
        paint: { 'line-color': '#8499A7', 'line-width': 4, 'line-opacity': 0.78 },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      instance.addLayer({
        id: 'reference-covered-line',
        type: 'line',
        source: 'reference-covered',
        paint: { 'line-color': '#2B8C76', 'line-width': 5, 'line-opacity': 0.95 },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      mapLoaded.current = true
      setMapReady(true)
    })
    return () => {
      mapLoaded.current = false
      setMapReady(false)
      framedKey.current = null
      instance.remove()
      map.current = null
    }
  }, [])

  const selectedDetail =
    routeId && competitionId ? details[detailKey(competitionId, routeId)] : undefined
  const selectedStage = selectedDetail?.stages.find((stage) => stage.route_id === stageId)
  const selectedGeometry = selectedStage?.geometry ?? selectedDetail?.geometry
  const selectedProjection = selectedStage?.[mode] ?? selectedDetail?.[mode]
  const selectedSummary = routes.find((route) => route.id === routeId)
  const groups = useMemo(
    () => [
      { key: 'via_czechia', label: copy.gameCompletionViaCzechia },
      { key: 'osm_numbered', label: copy.gameCompletionNumberedRoutes },
    ],
    [copy.gameCompletionNumberedRoutes, copy.gameCompletionViaCzechia],
  )

  useEffect(() => {
    if (!mapReady || !mapLoaded.current || !selectedGeometry) return
    const routeSource = map.current?.getSource('reference-route') as GeoJSONSource | undefined
    const coveredSource = map.current?.getSource('reference-covered') as GeoJSONSource | undefined
    routeSource?.setData({
      type: 'FeatureCollection',
      features: [completionFeature(selectedGeometry)],
    })
    const coveredGeometry = selectedProjection?.covered_geometry
    const coveredData =
      coveredGeometry &&
      typeof coveredGeometry === 'object' &&
      'type' in coveredGeometry &&
      coveredGeometry.type === 'FeatureCollection'
        ? coveredGeometry
        : {
            type: 'FeatureCollection',
            features: coveredGeometry ? [completionFeature(coveredGeometry)] : [],
          }
    coveredSource?.setData(coveredData as never)
    const key = `${competitionId ?? ''}:${routeId}:${stageId ?? ''}`
    if (framedKey.current === key) return
    const points = coordinates(selectedGeometry)
    if (points.length > 1) {
      const lngs = points.map(([lng]) => lng)
      const lats = points.map(([, lat]) => lat)
      map.current?.fitBounds(
        [
          [Math.min(...lngs), Math.min(...lats)],
          [Math.max(...lngs), Math.max(...lats)],
        ],
        { padding: 90, duration: 0 },
      )
    }
    framedKey.current = key
  }, [competitionId, mapReady, routeId, selectedGeometry, selectedProjection, stageId])

  const selectRoute = (id: string) => {
    setRouteId(id)
    setStageId(undefined)
  }
  const selectByKeyboard = (index: number, event: React.KeyboardEvent) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const next = event.key === 'ArrowDown' ? index + 1 : index - 1
    const item = routes[next]
    if (item) selectRoute(item.id)
  }

  const attribution = selectedDetail?.attribution ?? {}
  const attributionText =
    typeof attribution.attribution_text === 'string' ? attribution.attribution_text : ''
  const attributionLicence = typeof attribution.licence === 'string' ? attribution.licence : ''
  const currentMonthDistance = selectedProjection?.monthly?.find(
    (item) => item.month.slice(0, 7) === currentMonth,
  )

  if (signedOut)
    return (
      <div className="completion-empty">
        <strong>{copy.gameMapSignedOut}</strong>
        <p>{copy.gameMapSignedOutDescription}</p>
      </div>
    )

  return (
    <section className="completion-layout" aria-labelledby="game-completion-title">
      <aside className="completion-ledger">
        <div className="completion-header">
          <div className="completion-mode" role="tablist" aria-label="Game view">
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'player'}
              className={mode === 'player' ? 'is-active' : ''}
              onClick={() => setMode('player')}
            >
              {copy.gameCompletionPlayer}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'competition'}
              className={mode === 'competition' ? 'is-active' : ''}
              onClick={() => setMode('competition')}
            >
              {copy.gameCompletionGroup}
            </button>
          </div>
          <span className="game-account-kicker">{copy.gameCompletionKicker}</span>
          <h1 id="game-completion-title">{copy.gameCompletionTitle}</h1>
          <p>{copy.gameCompletionDescription}</p>
          {competitions.length > 0 && (
            <label className="game-map-label" htmlFor="completion-competition">
              {copy.gameMapCompetition}
              <select
                id="completion-competition"
                value={competitionId ?? ''}
                onChange={(event) => setCompetitionId(event.target.value)}
              >
                {competitions.map((competition) => (
                  <option value={competition.id} key={competition.id}>
                    {competition.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <div className="completion-list" aria-label={copy.gameCompletionRoutes}>
          {loading ? (
            <p role="status">{copy.gameCompletionLoading}</p>
          ) : error && routes.length === 0 ? (
            <div className="completion-state" role="alert">
              <p>{error}</p>
              <button type="button" onClick={() => void loadRoutes()}>
                {copy.gameCompletionRetry}
              </button>
            </div>
          ) : routes.length === 0 ? (
            <p>{copy.gameCompletionEmpty}</p>
          ) : (
            <>
              {catalogueIncomplete && (
                <p className="completion-state" role="alert">
                  {copy.gameCompletionCatalogueIncomplete}
                </p>
              )}
              {groups.map((group) => {
                const items = routes.filter(
                  (route) => (route.source_kind ?? 'osm_numbered') === group.key,
                )
                if (!items.length) return null
                return (
                  <div key={group.key} className="completion-group">
                    <h2>{group.label}</h2>
                    {items.map((route) => {
                      const detail = competitionId
                        ? details[detailKey(competitionId, route.id)]
                        : undefined
                      const projection = detail?.[mode]
                      const routeIndex = routes.indexOf(route)
                      return (
                        <div key={route.id} className="completion-route-wrap">
                          <button
                            type="button"
                            className={`completion-route ${route.id === routeId && !stageId ? 'is-selected' : ''}`}
                            aria-pressed={route.id === routeId && !stageId}
                            onClick={() => selectRoute(route.id)}
                            onKeyDown={(event) => selectByKeyboard(routeIndex, event)}
                          >
                            <span>
                              <b>{route.route_number || '—'}</b> {route.title}
                            </span>
                            <strong>{completionPercent(projection)}</strong>
                          </button>
                          {route.id === routeId && detail?.stages.length ? (
                            <div className="completion-stages">
                              <span>{copy.gameCompletionStages}</span>
                              {detail.stages.map((stage) => (
                                <button
                                  type="button"
                                  key={stage.route_id}
                                  className={stage.route_id === stageId ? 'is-selected' : ''}
                                  aria-pressed={stage.route_id === stageId}
                                  onClick={() => setStageId(stage.route_id)}
                                >
                                  <span>
                                    {stage.route_number || '—'} {stage.title}
                                  </span>
                                  <strong>{completionPercent(stage[mode])}</strong>
                                </button>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      )
                    })}
                  </div>
                )
              })}
            </>
          )}
          <small>{copy.gameCompletionKeyboardHint}</small>
        </div>
      </aside>
      <section className="completion-map-stage" aria-label={copy.gameMapInteractive}>
        <div
          ref={mapNode}
          className="game-map-canvas"
          role="application"
          aria-label={copy.gameMapInteractive}
        />
        {detailLoading && (
          <span className="game-map-loading" role="status">
            {copy.gameCompletionLoading}
          </span>
        )}
        {selectedSummary && detailError && (
          <aside className="completion-detail completion-detail-error">
            <p role="alert">{detailError}</p>
            <button type="button" onClick={() => routeId && void loadDetail(routeId)}>
              {copy.gameCompletionRetry}
            </button>
          </aside>
        )}
        {selectedSummary && selectedProjection && !detailError && (
          <aside className="completion-detail">
            <span className="game-account-kicker">{copy.gameCompletionReference}</span>
            <h2>{selectedStage?.title ?? selectedDetail?.title ?? selectedSummary.title}</h2>
            <div className="completion-percent">
              <strong>{completionPercent(selectedProjection)}</strong>
              <span>{copy.gameCompletionCovered}</span>
            </div>
            {statusLabel(selectedProjection.status, copy) && (
              <p className="completion-status">{statusLabel(selectedProjection.status, copy)}</p>
            )}
            {selectedProjection.partial && (
              <p className="completion-status completion-partial">{copy.gameCompletionPartial}</p>
            )}
            <dl className="completion-stats">
              <div>
                <dt>{copy.distance}</dt>
                <dd>{Number(selectedProjection.total_length_meters).toFixed(1)} m</dd>
              </div>
              <div>
                <dt>{copy.gameCompletionNewDistance}</dt>
                <dd>
                  {currentMonthDistance
                    ? `${Number(currentMonthDistance.covered_length_meters).toFixed(1)} m`
                    : '—'}
                </dd>
              </div>
            </dl>
            <div className="completion-legend">
              <span>
                <i className="completion-line covered" />
                {copy.gameCompletionCoveredLine}
              </span>
              <span>
                <i className="completion-line incomplete" />
                {copy.gameCompletionIncomplete}
              </span>
            </div>
            {attributionText && (
              <p className="completion-attribution">
                <strong>{copy.gameCompletionAttribution}</strong>
                <br />
                {attributionText}
                {attributionLicence ? ` · ${attributionLicence}` : ''}
              </p>
            )}
          </aside>
        )}
      </section>
    </section>
  )
}
