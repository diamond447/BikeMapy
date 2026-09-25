/* eslint-disable react-refresh/only-export-components -- map fixture helper is unit-tested. */
import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AttributionControl,
  Map as MapLibreMap,
  NavigationControl,
  setWorkerUrl,
} from 'maplibre-gl'
import type { FilterSpecification, GeoJSONSource, MapSourceDataEvent } from 'maplibre-gl'

import { apiClient, rememberCsrfToken } from './api/client'
import type { components } from './api/generated/schema'
import { GameAccount } from './components/GameAccount'
import { MAP_PROVIDER } from './mapProvider'
import { setMapLibreWorker } from './maplibreWorker'
import { translations } from './i18n/translations'
import type { Copy, Language } from './i18n/types'

type Competition = components['schemas']['Competition']
type MapResponse = components['schemas']['CompetitionMapResponse']
type MapActivity = components['schemas']['CompetitionMapActivity']

const STORAGE_KEY = 'bikemapy:game-map'
const DEFAULT_VIEW = { longitude: 16.6, latitude: 49.2, zoom: 7.5 }
function readPrivateState(): { competitionId?: string; members?: number[] } {
  try {
    const value = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '{}') as unknown
    if (!value || typeof value !== 'object') return {}
    const state = value as { competitionId?: unknown; members?: unknown }
    return {
      competitionId: typeof state.competitionId === 'string' ? state.competitionId : undefined,
      members: Array.isArray(state.members)
        ? state.members.filter((id): id is number => typeof id === 'number')
        : undefined,
    }
  } catch {
    return {}
  }
}

function writePrivateState(competitionId: string | undefined, members: number[] | undefined) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ competitionId, members }))
  } catch {
    /* Private browsing can disable session storage. */
  }
}

export function featureCollection(
  activities: MapActivity[],
  colors: globalThis.Map<number, string> = new globalThis.Map(),
) {
  return {
    type: 'FeatureCollection' as const,
    features: activities.map((activity) => ({
      type: 'Feature' as const,
      id: activity.id,
      properties: {
        player_id: activity.player_id,
        calendar_date: activity.calendar_date,
        color: colors.get(activity.player_id) ?? '#2B8C76',
      },
      geometry: activity.geometry,
    })),
  }
}

export function visibleMapData(data: MapResponse, memberIds: number[]): MapResponse {
  const selected = new Set(memberIds)
  return {
    ...data,
    members: data.members.filter((member) => selected.has(member.player_id)),
    activities: data.activities.filter((activity) => selected.has(activity.player_id)),
  }
}

function memberFilter(memberIds: number[]): FilterSpecification {
  return memberIds.length
    ? ['match', ['get', 'player_id'], memberIds, true, false]
    : ['==', ['get', 'player_id'], -1]
}

export function memberFeatureCollection(
  activities: MapActivity[],
  colors: globalThis.Map<number, string> = new globalThis.Map(),
) {
  const grouped = new globalThis.Map<number, number[][][]>()
  for (const activity of activities) {
    const geometry = activity.geometry as { type?: unknown; coordinates?: unknown }
    if (geometry.type === 'LineString' && Array.isArray(geometry.coordinates)) {
      const lines = grouped.get(activity.player_id) ?? []
      lines.push(geometry.coordinates as number[][])
      grouped.set(activity.player_id, lines)
    } else if (geometry.type === 'MultiLineString' && Array.isArray(geometry.coordinates)) {
      const lines = grouped.get(activity.player_id) ?? []
      lines.push(...(geometry.coordinates as number[][][]))
      grouped.set(activity.player_id, lines)
    }
  }
  return {
    type: 'FeatureCollection' as const,
    features: [...grouped].map(([playerId, coordinates]) => ({
      type: 'Feature' as const,
      id: `member-${playerId}`,
      properties: {
        player_id: playerId,
        color: colors.get(playerId) ?? '#2B8C76',
      },
      geometry: { type: 'MultiLineString' as const, coordinates },
    })),
  }
}

function statusMessage(status: MapResponse['status'], copy: Copy) {
  if (status === 'syncing') return copy.gameMapSyncing
  if (status === 'empty') return copy.gameMapEmpty
  return copy.gameMapLoaded
}

export default function GameApp() {
  const [language, setLanguage] = useState<Language>(() =>
    navigator.language.toLowerCase().startsWith('cs') ? 'cs' : 'en',
  )
  const copy = translations[language]
  const [competitions, setCompetitions] = useState<Competition[]>([])
  const [competitionId, setCompetitionId] = useState<string | undefined>(
    () => readPrivateState().competitionId,
  )
  const [visibleMembers, setVisibleMembers] = useState<number[] | null>(
    () => readPrivateState().members ?? null,
  )
  const [mapData, setMapData] = useState<MapResponse | null>(null)
  const [mapDataIncludesAllMembers, setMapDataIncludesAllMembers] = useState(false)
  const [loading, setLoading] = useState(true)
  const [mapLoading, setMapLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [signedOut, setSignedOut] = useState(false)
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null)
  const [mapReady, setMapReady] = useState(false)
  const traceCloseRef = useRef<HTMLButtonElement>(null)
  const traceButtonRefs = useRef(new Map<string, HTMLButtonElement>())
  const originatingTraceRef = useRef<string | null>(null)
  const mapNode = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const mapLoaded = useRef(false)
  const mapCanRequest = useRef(false)
  const mapRequestInFlight = useRef(false)
  const lastMapRequestKey = useRef<string | null>(null)
  const latestMapRequestKey = useRef<string | null>(null)
  const mapRequestPending = useRef(false)
  const mapMeasureSequence = useRef(0)
  const visibleMemberIdsRef = useRef<number[]>([])
  const fullMapDataRef = useRef<MapResponse | null>(null)
  const fullMapRequestKey = useRef<string | null>(null)
  const bounds = useRef({ west: 14, south: 48.5, east: 19, north: 51.2, zoom: 7.5 })
  const loadMapRef = useRef<() => Promise<void>>(() => Promise.resolve())
  const mapLoadTimer = useRef<number | null>(null)

  const scheduleMapLoad = useCallback(() => {
    if (mapLoadTimer.current !== null) window.clearTimeout(mapLoadTimer.current)
    mapLoadTimer.current = window.setTimeout(() => {
      mapLoadTimer.current = null
      void loadMapRef.current()
    }, 250)
  }, [])

  const loadCompetitions = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await apiClient.GET('/api/v1/game/competitions/', { credentials: 'include' })
      rememberCsrfToken(result.response)
      if (result.response?.status === 401) {
        setSignedOut(true)
        setCompetitions([])
        return
      }
      if (!result.data) throw new Error('competition-load')
      setSignedOut(false)
      setCompetitions(result.data.competitions)
      setCompetitionId((current) => {
        const selected = result.data.competitions.find((item) => item.id === current)
        return selected?.id ?? result.data.active_competition_id ?? result.data.competitions[0]?.id
      })
    } catch {
      setError(copy.gameMapError)
    } finally {
      setLoading(false)
    }
  }, [copy.gameMapError])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadCompetitions(), 0)
    return () => window.clearTimeout(timer)
  }, [loadCompetitions])

  const loadMap = useCallback(async () => {
    if (!competitionId || !map.current || !mapLoaded.current || !mapCanRequest.current) return
    setError(null)
    const current = bounds.current
    const selectedCompetition = competitions.find((competition) => competition.id === competitionId)
    const availableMembers = selectedCompetition?.members.map((member) => member.player_id) ?? []
    const selected =
      visibleMembers === null
        ? availableMembers
        : visibleMembers.filter((id) => availableMembers.includes(id))
    // A competition list contains at most the map endpoint's member cap in
    // the normal path. Cache that authorized response and change visibility
    // with a layer filter instead of reparsing 1,200 traces for every toggle.
    // Larger competitions retain the server-filtered path below.
    const canCacheAllMembers = availableMembers.length <= 100
    const requestedMembers = canCacheAllMembers ? availableMembers : selected
    const requestKey = JSON.stringify([
      competitionId,
      Math.round(current.west * 1000),
      Math.round(current.south * 1000),
      Math.round(current.east * 1000),
      Math.round(current.north * 1000),
      Math.round(current.zoom * 10),
      requestedMembers,
    ])
    latestMapRequestKey.current = requestKey
    if (canCacheAllMembers && fullMapRequestKey.current === requestKey && fullMapDataRef.current) {
      const sequence = mapMeasureSequence.current++
      const startMark = `game-map-update-${sequence}`
      performance.mark(startMark)
      const onRender = () => {
        map.current?.off('render', onRender)
        window.requestAnimationFrame(() => {
          performance.measure('game-map-update-to-render', startMark)
          performance.clearMarks(startMark)
        })
      }
      map.current?.on('render', onRender)
      visibleMemberIdsRef.current = selected
      map.current?.setFilter('private-traces-visual', memberFilter(selected))
      return
    }
    if (mapRequestInFlight.current) {
      if (lastMapRequestKey.current !== requestKey) mapRequestPending.current = true
      return
    }
    if (lastMapRequestKey.current === requestKey) return
    lastMapRequestKey.current = requestKey
    mapRequestInFlight.current = true
    setMapLoading(true)
    try {
      const result = await apiClient.GET('/api/v1/game/competitions/{competition_id}/map/', {
        params: {
          path: { competition_id: competitionId },
          query: {
            west: current.west,
            south: current.south,
            east: current.east,
            north: current.north,
            zoom: Math.round(current.zoom),
            member: requestedMembers.length ? requestedMembers : [0],
          },
        },
        credentials: 'include',
      })
      if (latestMapRequestKey.current !== requestKey) return
      if (result.response?.status === 401) {
        setSignedOut(true)
        return
      }
      if (result.response?.status === 404) throw new Error('map-not-found')
      if (!result.data) throw new Error('map-load')
      if (canCacheAllMembers) {
        fullMapDataRef.current = result.data
        fullMapRequestKey.current = requestKey
      } else {
        fullMapDataRef.current = null
        fullMapRequestKey.current = null
      }
      setMapDataIncludesAllMembers(canCacheAllMembers)
      startTransition(() =>
        setMapData(canCacheAllMembers ? result.data : visibleMapData(result.data, selected)),
      )
      const source = map.current?.getSource('private-traces') as GeoJSONSource | undefined
      const visualSource = map.current?.getSource('private-traces-visual') as
        GeoJSONSource | undefined
      const colors = new globalThis.Map(
        competitions
          .find((competition) => competition.id === competitionId)
          ?.members.map((member) => [member.player_id, member.color] as [number, string]),
      )
      if (source && map.current) {
        const sequence = mapMeasureSequence.current++
        const startMark = `game-map-response-${sequence}`
        performance.mark(startMark)
        const onSourceData = (event: MapSourceDataEvent) => {
          if (event.sourceId !== 'private-traces-visual' || !event.isSourceLoaded) return
          map.current?.off('sourcedata', onSourceData)
          window.requestAnimationFrame(() => {
            performance.measure('game-map-update-to-render', startMark)
            performance.clearMarks(startMark)
          })
        }
        map.current.on('sourcedata', onSourceData)
        source.setData(featureCollection(result.data.activities, colors))
        visualSource?.setData(memberFeatureCollection(result.data.activities, colors))
        visibleMemberIdsRef.current = selected
        map.current.setFilter('private-traces-visual', memberFilter(selected))
      }
    } catch {
      if (latestMapRequestKey.current === requestKey) {
        // Permit the visible Retry action to resend the same viewport after a
        // failed request; successful requests remain deduplicated.
        lastMapRequestKey.current = null
        setError(copy.gameMapError)
      }
    } finally {
      const followUp = mapRequestPending.current || latestMapRequestKey.current !== requestKey
      mapRequestPending.current = false
      mapRequestInFlight.current = false
      setMapLoading(false)
      if (followUp) scheduleMapLoad()
    }
  }, [competitionId, competitions, copy.gameMapError, scheduleMapLoad, visibleMembers])

  useEffect(() => {
    loadMapRef.current = loadMap
  }, [loadMap])

  useEffect(() => {
    if (!mapNode.current || map.current) return
    setMapLibreWorker(setWorkerUrl)
    const instance = new MapLibreMap({
      container: mapNode.current,
      style: MAP_PROVIDER.style,
      center: [DEFAULT_VIEW.longitude, DEFAULT_VIEW.latitude] as [number, number],
      zoom: DEFAULT_VIEW.zoom,
      attributionControl: false,
      keyboard: true,
    })
    map.current = instance
    instance.addControl(new NavigationControl({ showCompass: true }), 'top-right')
    instance.addControl(new AttributionControl({ customAttribution: MAP_PROVIDER.attribution }))
    instance.on('moveend', () => {
      const current = instance.getBounds()
      bounds.current = {
        west: current.getWest(),
        south: current.getSouth(),
        east: current.getEast(),
        north: current.getNorth(),
        zoom: instance.getZoom(),
      }
      if (!mapCanRequest.current) return
      scheduleMapLoad()
    })
    instance.on('load', () => {
      mapLoaded.current = true
      const current = instance.getBounds()
      bounds.current = {
        west: current.getWest(),
        south: current.getSouth(),
        east: current.getEast(),
        north: current.getNorth(),
        zoom: instance.getZoom(),
      }
      // The endpoint clips traces to the current viewport and simplifies them
      // for the requested zoom.  Avoid copying that bounded data into adjacent
      // worker tiles, and let the worker apply its normal line simplification;
      // both reduce the update cost without changing the private payload or
      // the rendered layer's interaction contract.
      instance.addSource('private-traces', {
        type: 'geojson',
        data: featureCollection([]),
        buffer: 0,
        tolerance: 1,
      })
      instance.addSource('private-traces-visual', {
        type: 'geojson',
        data: memberFeatureCollection([]),
        buffer: 0,
        tolerance: 1,
      })
      instance.addLayer({
        id: 'private-traces-visual',
        type: 'line',
        source: 'private-traces-visual',
        paint: {
          'line-color': ['coalesce', ['get', 'color'], '#2B8C76'],
          'line-width': ['interpolate', ['linear'], ['zoom'], 5, 2, 14, 4],
          'line-opacity': 0.78,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      instance.addLayer({
        id: 'private-traces',
        type: 'line',
        source: 'private-traces',
        paint: {
          'line-color': '#2B8C76',
          'line-width': ['interpolate', ['linear'], ['zoom'], 5, 2, 14, 4],
          // Keep the activity-level source available for precise trace hit
          // testing while the grouped visual source handles rendering.
          'line-opacity': 0,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      setMapReady(true)
      instance.on('click', 'private-traces', (event) => {
        const id = event.features?.[0]?.id
        const playerId = event.features?.[0]?.properties?.player_id
        if (id !== undefined && visibleMemberIdsRef.current.includes(Number(playerId))) {
          originatingTraceRef.current = String(id)
          setSelectedTraceId(String(id))
        }
      })
      instance.on('mouseenter', 'private-traces', () => {
        instance.getCanvas().style.cursor = 'pointer'
      })
      instance.on('mouseleave', 'private-traces', () => {
        instance.getCanvas().style.cursor = ''
      })
      instance.once('idle', () => {
        mapCanRequest.current = true
        scheduleMapLoad()
      })
    })
    return () => {
      mapLoaded.current = false
      mapCanRequest.current = false
      mapRequestInFlight.current = false
      lastMapRequestKey.current = null
      latestMapRequestKey.current = null
      mapRequestPending.current = false
      fullMapDataRef.current = null
      fullMapRequestKey.current = null
      setMapDataIncludesAllMembers(false)
      if (mapLoadTimer.current !== null) window.clearTimeout(mapLoadTimer.current)
      mapLoadTimer.current = null
      instance.remove()
      map.current = null
    }
  }, [scheduleMapLoad])

  useEffect(() => {
    if (!competitionId) return
    const timer = window.setTimeout(() => void loadMap(), 0)
    return () => window.clearTimeout(timer)
  }, [competitionId, loadMap])

  useEffect(() => {
    writePrivateState(competitionId, visibleMembers ?? undefined)
  }, [competitionId, visibleMembers])

  useEffect(() => {
    if (selectedTraceId) traceCloseRef.current?.focus()
  }, [selectedTraceId])

  const selectedCompetition = useMemo(
    () => competitions.find((competition) => competition.id === competitionId),
    [competitionId, competitions],
  )
  const traceActivities = useMemo(() => mapData?.activities ?? [], [mapData])
  const selectedTrace = useMemo(() => {
    const candidate = traceActivities.find((activity) => activity.id === selectedTraceId)
    if (
      !candidate ||
      visibleMembers === null ||
      !mapDataIncludesAllMembers ||
      visibleMembers.includes(candidate.player_id)
    ) {
      return candidate
    }
    return undefined
  }, [mapDataIncludesAllMembers, selectedTraceId, traceActivities, visibleMembers])
  const traceButtons = useMemo(
    () =>
      traceActivities.map((activity) => (
        <button
          type="button"
          key={activity.id}
          data-player-id={activity.player_id}
          ref={(node) => {
            if (node) traceButtonRefs.current.set(activity.id, node)
            else traceButtonRefs.current.delete(activity.id)
          }}
          aria-pressed={selectedTraceId === activity.id}
          onClick={() => {
            originatingTraceRef.current = activity.id
            setSelectedTraceId(activity.id)
          }}
        >
          <span
            className="member-swatch"
            style={{
              backgroundColor:
                selectedCompetition?.members.find(
                  (member) => member.player_id === activity.player_id,
                )?.color ?? '#2B8C76',
            }}
            aria-hidden="true"
          />
          <span>{activity.calendar_date ?? copy.gameMapDateUnknown}</span>
        </button>
      )),
    [copy.gameMapDateUnknown, selectedCompetition, selectedTraceId, traceActivities],
  )
  const traceListRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const selected = visibleMembers === null ? null : new Set(visibleMembers)
    traceListRef.current
      ?.querySelectorAll<HTMLButtonElement>('[data-player-id]')
      .forEach((button) => {
        button.hidden = selected !== null && !selected.has(Number(button.dataset.playerId))
      })
  }, [traceActivities, visibleMembers])
  const closeTrace = useCallback(() => {
    const originatingId = originatingTraceRef.current
    setSelectedTraceId(null)
    window.setTimeout(() => {
      const button = originatingId ? traceButtonRefs.current.get(originatingId) : undefined
      if (button) button.focus()
      else map.current?.getCanvas().focus()
    }, 0)
  }, [])
  const toggleMember = (id: number) =>
    setVisibleMembers((current) => {
      const members =
        current ?? selectedCompetition?.members.map((member) => member.player_id) ?? []
      return members.includes(id) ? members.filter((item) => item !== id) : [...members, id]
    })

  return (
    <main className="game-shell">
      <header className="game-topbar">
        <a className="wordmark" href="/" aria-label={copy.home}>
          <span className="wordmark-mark" aria-hidden="true">
            ↗
          </span>
          <span>BikeMapy</span>
        </a>
        <div className="game-topbar-actions">
          <button
            type="button"
            className="language-toggle"
            onClick={() => setLanguage((current) => (current === 'en' ? 'cs' : 'en'))}
            aria-label={copy.changeLanguage}
          >
            {language.toUpperCase()}
          </button>
          <GameAccount copy={copy} />
        </div>
      </header>
      <section className="game-map-layout" aria-labelledby="game-map-title">
        <aside className="game-map-controls">
          <span className="game-account-kicker">{copy.gameMapKicker}</span>
          <h1 id="game-map-title">{copy.gameMapTitle}</h1>
          <p>{copy.gameMapDescription}</p>
          {signedOut ? (
            <div className="game-map-state">
              <strong>{copy.gameMapSignedOut}</strong>
              <p>{copy.gameMapSignedOutDescription}</p>
            </div>
          ) : loading ? (
            <p role="status">{copy.gameMapLoading}</p>
          ) : error ? (
            <div className="game-map-state">
              <p role="alert">{error}</p>
              <button type="button" onClick={() => void loadCompetitions()}>
                {copy.gameMapRetry}
              </button>
            </div>
          ) : competitions.length === 0 ? (
            <p>{copy.gameMapNoCompetitions}</p>
          ) : (
            <>
              <label className="game-map-label" htmlFor="game-competition">
                {copy.gameMapCompetition}
              </label>
              <select
                id="game-competition"
                value={competitionId ?? ''}
                onChange={(event) => setCompetitionId(event.target.value)}
              >
                {competitions.map((competition) => (
                  <option value={competition.id} key={competition.id}>
                    {competition.name}
                  </option>
                ))}
              </select>
              <fieldset className="game-member-list">
                <legend>{copy.gameMapMembers}</legend>
                {selectedCompetition?.members.map((member) => (
                  <label key={member.player_id}>
                    <input
                      type="checkbox"
                      checked={visibleMembers === null || visibleMembers.includes(member.player_id)}
                      onChange={() => toggleMember(member.player_id)}
                    />
                    <span
                      className="member-swatch"
                      style={{ backgroundColor: member.color }}
                      aria-hidden="true"
                    />
                    {member.nickname || member.display_name}
                  </label>
                ))}
              </fieldset>
              {mapData && (
                <p className="game-map-status" role="status">
                  {statusMessage(mapData.status, copy)}
                  {mapData.truncated ? ` ${copy.gameMapTruncated}` : ''}
                </p>
              )}
              {mapData && mapData.activities.length > 0 && (
                <div className="game-trace-list" aria-label={copy.gameMapTraceList}>
                  <h2>{copy.gameMapTraceList}</h2>
                  <div ref={traceListRef} className="game-trace-viewport">
                    {traceButtons}
                  </div>
                </div>
              )}
            </>
          )}
        </aside>
        <section
          className="game-map-stage"
          aria-label={copy.gameMapInteractive}
          data-map-source-loaded={mapReady ? 'true' : 'false'}
          data-map-response-loaded={mapData ? 'true' : 'false'}
        >
          <div
            ref={mapNode}
            className="game-map-canvas"
            role="application"
            aria-label={copy.gameMapInteractive}
          />
          {mapLoading && (
            <span className="game-map-loading" role="status">
              {copy.gameMapLoading}
            </span>
          )}
          {selectedTrace && (
            <aside
              className="game-trace-detail"
              aria-label={copy.gameMapTraceDetail}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault()
                  closeTrace()
                }
              }}
            >
              <button
                ref={traceCloseRef}
                type="button"
                className="game-trace-close"
                onClick={closeTrace}
                aria-label={copy.gameMapCloseTrace}
              >
                ×
              </button>
              <span className="game-account-kicker">{copy.gameMapTraceDetail}</span>
              <strong>{copy.gameMapRideDate}</strong>
              <time dateTime={selectedTrace.calendar_date ?? undefined}>
                {selectedTrace.calendar_date ?? copy.gameMapDateUnknown}
              </time>
            </aside>
          )}
        </section>
      </section>
    </main>
  )
}
