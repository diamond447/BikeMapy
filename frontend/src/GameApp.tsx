/* eslint-disable react-refresh/only-export-components -- map fixture helper is unit-tested. */
import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AttributionControl,
  Map as MapLibreMap,
  NavigationControl,
  setWorkerUrl,
} from 'maplibre-gl'
import type { FilterSpecification } from 'maplibre-gl'

import { apiClient, rememberCsrfToken } from './api/client'
import type { components } from './api/generated/schema'
import { GameAccount } from './components/GameAccount'
import { subscribeToGameDataRefresh, subscribeToGameMapReset } from './gameState'
import { MAP_PROVIDER } from './mapProvider'
import { setMapLibreWorker } from './maplibreWorker'
import { translations } from './i18n/translations'
import type { Copy, Language } from './i18n/types'

type Competition = components['schemas']['Competition']
type MapResponse = components['schemas']['CompetitionMapResponse']
type MapActivity = components['schemas']['CompetitionMapActivity']

const STORAGE_KEY = 'bikemapy:game-map'
const TRACE_PAGE_SIZE = 100
export const MAX_RENDER_COORDINATES = 2_400
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

type InteractionPoint = { lng: number; lat: number }

function lineParts(activity: MapActivity): number[][][] {
  const geometry = activity.geometry as { type?: unknown; coordinates?: unknown }
  if (geometry.type === 'LineString' && Array.isArray(geometry.coordinates)) {
    return [geometry.coordinates as number[][]]
  }
  if (geometry.type === 'MultiLineString' && Array.isArray(geometry.coordinates)) {
    return geometry.coordinates as number[][][]
  }
  return []
}

function segmentDistanceSquared(point: InteractionPoint, start: number[], end: number[]): number {
  const scale = Math.cos((point.lat * Math.PI) / 180)
  const px = point.lng * scale
  const py = point.lat
  const ax = (start[0] ?? 0) * scale
  const ay = start[1] ?? 0
  const bx = (end[0] ?? 0) * scale
  const by = end[1] ?? 0
  const dx = bx - ax
  const dy = by - ay
  const lengthSquared = dx * dx + dy * dy
  if (lengthSquared === 0) return (px - ax) ** 2 + (py - ay) ** 2
  const position = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSquared))
  const closestX = ax + position * dx
  const closestY = ay + position * dy
  return (px - closestX) ** 2 + (py - closestY) ** 2
}

/** Resolve a grouped member line click to the nearest authorized activity. */
export function nearestActivityId(
  activities: MapActivity[],
  visibleMemberIds: number[],
  point: InteractionPoint,
  maxDistance = Number.POSITIVE_INFINITY,
): string | null {
  const visible = new Set(visibleMemberIds)
  let closestId: string | null = null
  let closestDistance = Number.POSITIVE_INFINITY
  for (const activity of activities) {
    if (!visible.has(activity.player_id)) continue
    for (const line of lineParts(activity)) {
      for (let index = 1; index < line.length; index += 1) {
        const distance = segmentDistanceSquared(point, line[index - 1] ?? [], line[index] ?? [])
        if (distance < closestDistance) {
          closestDistance = distance
          closestId = activity.id
        }
      }
    }
  }
  return closestDistance <= maxDistance * maxDistance ? closestId : null
}

export function memberFilter(memberIds: number[]): FilterSpecification {
  return memberIds.length
    ? ['match', ['get', 'player_id'], memberIds, true, false]
    : ['==', ['get', 'player_id'], -1]
}

type RenderGeometryOptions = { maxCoordinates?: number; zoom?: number }

function unwrapLongitude(longitude: number, reference: number): number {
  let result = longitude
  while (result - reference > 180) result -= 360
  while (result - reference < -180) result += 360
  return result
}

function pointDistanceSquared(point: number[], start: number[], end: number[], scale: number) {
  const px = unwrapLongitude(point[0] ?? 0, start[0] ?? 0) * scale
  const py = point[1] ?? 0
  const ax = (start[0] ?? 0) * scale
  const ay = start[1] ?? 0
  const bx = unwrapLongitude(end[0] ?? 0, start[0] ?? 0) * scale
  const by = end[1] ?? 0
  const dx = bx - ax
  const dy = by - ay
  const lengthSquared = dx * dx + dy * dy
  if (lengthSquared === 0) return (px - ax) ** 2 + (py - ay) ** 2
  const position = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSquared))
  const closestX = ax + position * dx
  const closestY = ay + position * dy
  return (px - closestX) ** 2 + (py - closestY) ** 2
}

function simplifyLine(line: number[][], tolerance: number): number[][] {
  if (line.length <= 2) return line
  const keep = new Uint8Array(line.length)
  keep[0] = 1
  keep[line.length - 1] = 1
  const pending: [number, number][] = [[0, line.length - 1]]
  while (pending.length) {
    const [start, end] = pending.pop() as [number, number]
    const scale = Math.cos(
      ((((line[start]?.[1] ?? 0) + (line[end]?.[1] ?? 0)) / 2) * Math.PI) / 180,
    )
    let farthest = -1
    let farthestDistance = tolerance * tolerance
    for (let index = start + 1; index < end; index += 1) {
      const distance = pointDistanceSquared(
        line[index] ?? [],
        line[start] ?? [],
        line[end] ?? [],
        scale,
      )
      if (distance > farthestDistance) {
        farthest = index
        farthestDistance = distance
      }
    }
    if (farthest >= 0) {
      keep[farthest] = 1
      pending.push([start, farthest], [farthest, end])
    }
  }
  return line.filter((_, index) => keep[index] === 1)
}

function decimateLine(line: number[][], target: number): number[][] {
  if (line.length <= target) return line
  return Array.from(
    { length: target },
    (_, index) => line[Math.round((index * (line.length - 1)) / (target - 1))] ?? [],
  )
}

function boundRenderLines(lines: number[][][], maxCoordinates: number): number[][][] {
  const total = lines.reduce((count, line) => count + line.length, 0)
  if (total <= maxCoordinates) return lines
  const minimum = lines.reduce((count, line) => count + Math.min(line.length, 2), 0)
  const extraBudget = Math.max(0, maxCoordinates - minimum)
  const extraTotal = lines.reduce((count, line) => count + Math.max(0, line.length - 2), 0)
  const scale = extraTotal ? Math.min(1, extraBudget / extraTotal) : 0
  const targets = lines.map((line) =>
    Math.min(line.length, 2 + Math.floor(Math.max(0, line.length - 2) * scale)),
  )
  let allocated = targets.reduce((count, target) => count + target, 0)
  for (let index = 0; allocated < maxCoordinates && index < lines.length; index += 1) {
    if (targets[index] < lines[index]?.length) {
      targets[index] = (targets[index] ?? 0) + 1
      allocated += 1
    }
    if (index === lines.length - 1 && allocated < maxCoordinates) index = -1
  }
  return lines.map((line, index) => decimateLine(line, targets[index] ?? 2))
}

export function memberFeatureCollection(
  activities: MapActivity[],
  colors: globalThis.Map<number, string> = new globalThis.Map(),
  options: RenderGeometryOptions = {},
) {
  const grouped = new globalThis.Map<number, number[][][]>()
  const tolerance = 0.00005 * 2 ** Math.max(0, 12 - (options.zoom ?? 12))
  for (const activity of activities) {
    const geometry = activity.geometry as { type?: unknown; coordinates?: unknown }
    if (geometry.type === 'LineString' && Array.isArray(geometry.coordinates)) {
      const lines = grouped.get(activity.player_id) ?? []
      lines.push(simplifyLine(geometry.coordinates as number[][], tolerance))
      grouped.set(activity.player_id, lines)
    } else if (geometry.type === 'MultiLineString' && Array.isArray(geometry.coordinates)) {
      const lines = grouped.get(activity.player_id) ?? []
      lines.push(
        ...(geometry.coordinates as number[][][]).map((line) => simplifyLine(line, tolerance)),
      )
      grouped.set(activity.player_id, lines)
    }
  }
  const bounded = boundRenderLines(
    [...grouped.values()].flat(),
    options.maxCoordinates ?? MAX_RENDER_COORDINATES,
  )
  let boundedIndex = 0
  return {
    type: 'FeatureCollection' as const,
    features: [...grouped].map(([playerId, coordinates]) => ({
      type: 'Feature' as const,
      id: `member-${playerId}`,
      properties: {
        player_id: playerId,
        color: colors.get(playerId) ?? '#2B8C76',
      },
      geometry: {
        type: 'MultiLineString' as const,
        coordinates: coordinates.map(() => bounded[boundedIndex++] ?? []),
      },
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
  // The benchmark exercises the complete response and source parsing path.
  // It is intentionally opt-in and never changes normal user interaction.
  const benchmarkFullUpdate =
    typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).get('benchmark') === 'full-update'
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
  const [traceListLimit, setTraceListLimit] = useState(TRACE_PAGE_SIZE)
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
  const mapInteractionActivitiesRef = useRef<MapActivity[]>([])
  const fullMapDataRef = useRef<MapResponse | null>(null)
  const fullMapRequestKey = useRef<string | null>(null)
  const visualDataRef = useRef(memberFeatureCollection([]))
  const drawVisualDataRef = useRef<() => void>(() => undefined)
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

  const clearMapData = useCallback(() => {
    fullMapDataRef.current = null
    fullMapRequestKey.current = null
    lastMapRequestKey.current = null
    latestMapRequestKey.current = null
    mapRequestPending.current = false
    mapRequestInFlight.current = false
    visibleMemberIdsRef.current = []
    mapInteractionActivitiesRef.current = []
    visualDataRef.current = memberFeatureCollection([])
    drawVisualDataRef.current()
    setMapData(null)
    setMapDataIncludesAllMembers(false)
    setSelectedTraceId(null)
    setTraceListLimit(TRACE_PAGE_SIZE)
    setError(null)
  }, [])

  const clearSessionState = useCallback(() => {
    clearMapData()
    setCompetitions([])
    setCompetitionId(undefined)
    setVisibleMembers(null)
  }, [clearMapData])

  const loadCompetitions = useCallback(async () => {
    clearMapData()
    setLoading(true)
    setError(null)
    try {
      const result = await apiClient.GET('/api/v1/game/competitions/', { credentials: 'include' })
      rememberCsrfToken(result.response)
      if (result.response?.status === 401) {
        clearSessionState()
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
  }, [clearMapData, clearSessionState, copy.gameMapError])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadCompetitions(), 0)
    return () => window.clearTimeout(timer)
  }, [loadCompetitions])

  useEffect(() => {
    const unsubscribeReset = subscribeToGameMapReset((event) => {
      if (event.detail?.reason === 'logout' || event.detail?.reason === 'auth-loss') {
        clearSessionState()
      } else {
        clearMapData()
      }
    })
    const unsubscribeRefresh = subscribeToGameDataRefresh(() => void loadCompetitions())
    return () => {
      unsubscribeReset()
      unsubscribeRefresh()
    }
  }, [clearMapData, clearSessionState, loadCompetitions])

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
    const canCacheAllMembers = !benchmarkFullUpdate && availableMembers.length <= 100
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
      drawVisualDataRef.current()
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
        clearSessionState()
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
      const processingStart = `game-map-response-to-source-${mapMeasureSequence.current}`
      performance.mark(processingStart)
      const colors = new globalThis.Map(
        competitions
          .find((competition) => competition.id === competitionId)
          ?.members.map((member) => [member.player_id, member.color] as [number, string]),
      )
      const sequence = mapMeasureSequence.current++
      const startMark = `game-map-response-${sequence}`
      performance.mark(startMark)
      mapInteractionActivitiesRef.current = result.data.activities
      visualDataRef.current = memberFeatureCollection(result.data.activities, colors, {
        maxCoordinates: MAX_RENDER_COORDINATES,
        zoom: current.zoom,
      })
      performance.measure('game-map-response-to-source', processingStart)
      performance.clearMarks(processingStart)
      const overlayUpdateStart = `game-map-overlay-update-${sequence}`
      performance.mark(overlayUpdateStart)
      visibleMemberIdsRef.current = selected
      drawVisualDataRef.current()
      performance.measure('game-map-overlay-update', overlayUpdateStart)
      performance.clearMarks(overlayUpdateStart)
      window.requestAnimationFrame(() => {
        performance.measure('game-map-update-to-render', startMark)
        performance.clearMarks(startMark)
      })
    } catch {
      if (latestMapRequestKey.current === requestKey) {
        // Permit the visible Retry action to resend the same viewport after a
        // failed request; successful requests remain deduplicated.
        lastMapRequestKey.current = null
        clearMapData()
        setError(copy.gameMapError)
      }
    } finally {
      const followUp = mapRequestPending.current || latestMapRequestKey.current !== requestKey
      mapRequestPending.current = false
      mapRequestInFlight.current = false
      setMapLoading(false)
      if (followUp) scheduleMapLoad()
    }
  }, [
    clearMapData,
    clearSessionState,
    benchmarkFullUpdate,
    competitionId,
    competitions,
    copy.gameMapError,
    scheduleMapLoad,
    visibleMembers,
  ])

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
    let benchmarkInteraction: ((event: Event) => void) | undefined
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
      if (benchmarkFullUpdate) {
        instance.jumpTo({ center: [14.5, 49], zoom: 8.5 })
      }
      const current = instance.getBounds()
      bounds.current = {
        west: current.getWest(),
        south: current.getSouth(),
        east: current.getEast(),
        north: current.getNorth(),
        zoom: instance.getZoom(),
      }
      // Activity geometry stays in the authorized response and is resolved on
      // demand for clicks. The bounded, tolerance-simplified grouped geometry
      // is drawn in an overlay canvas so a fresh response does not wait for a
      // GeoJSON worker parse before becoming visible. MapLibre still owns
      // navigation, attribution, projection, and the base map.
      const overlay = document.createElement('canvas')
      overlay.className = 'game-map-render-overlay'
      overlay.setAttribute('aria-hidden', 'true')
      overlay.style.pointerEvents = 'none'
      overlay.style.position = 'absolute'
      overlay.style.inset = '0'
      overlay.style.width = '100%'
      overlay.style.height = '100%'
      instance.getContainer().appendChild(overlay)
      // Keep an empty, hidden source as a stable style anchor. It is never
      // populated with activity geometry; the canvas above is the visual path.
      instance.addSource('private-traces-visual', {
        type: 'geojson',
        data: memberFeatureCollection([]),
      })
      instance.addLayer({
        id: 'private-traces-visual',
        type: 'line',
        source: 'private-traces-visual',
        layout: { visibility: 'none' },
        paint: { 'line-opacity': 0 },
      })
      const drawVisualData = () => {
        const mapCanvas = instance.getCanvas()
        const width = mapCanvas.clientWidth || instance.getContainer().clientWidth || 1024
        const height = mapCanvas.clientHeight || instance.getContainer().clientHeight || 768
        const scale = window.devicePixelRatio || 1
        if (
          overlay.width !== Math.round(width * scale) ||
          overlay.height !== Math.round(height * scale)
        ) {
          overlay.width = Math.round(width * scale)
          overlay.height = Math.round(height * scale)
        }
        const context = overlay.getContext('2d')
        if (!context) return
        context.setTransform(scale, 0, 0, scale, 0, 0)
        context.clearRect(0, 0, width, height)
        const visible = new Set(visibleMemberIdsRef.current)
        const lineWidth = Math.max(2, Math.min(4, (instance.getZoom() - 5) / 3 + 2))
        for (const feature of visualDataRef.current.features) {
          const playerId = Number(feature.properties?.player_id)
          if (!visible.has(playerId)) continue
          context.strokeStyle = String(feature.properties?.color ?? '#2B8C76')
          context.lineWidth = lineWidth
          context.lineCap = 'round'
          context.lineJoin = 'round'
          for (const line of feature.geometry.coordinates) {
            context.beginPath()
            let previousX: number | undefined
            let hasPoint = false
            for (const coordinate of line) {
              const projected = instance.project({
                lng: coordinate[0] ?? 0,
                lat: coordinate[1] ?? 0,
              })
              if (previousX !== undefined && Math.abs(projected.x - previousX) > width / 2) {
                context.stroke()
                context.beginPath()
                hasPoint = false
              }
              if (!hasPoint) {
                context.moveTo(projected.x, projected.y)
                hasPoint = true
              } else {
                context.lineTo(projected.x, projected.y)
              }
              previousX = projected.x
            }
            context.stroke()
          }
        }
      }
      drawVisualDataRef.current = drawVisualData
      instance.on('move', drawVisualData)
      instance.on('resize', drawVisualData)
      setMapReady(true)
      const selectActivityAtPoint = (point: { lng: number; lat: number }) => {
        const interactionSequence = mapMeasureSequence.current++
        const interactionStart = `game-map-lazy-interaction-${interactionSequence}`
        performance.mark(interactionStart)
        const id = nearestActivityId(
          mapInteractionActivitiesRef.current,
          visibleMemberIdsRef.current,
          point,
          0.03 / 2 ** Math.max(0, instance.getZoom() - 8),
        )
        if (id) {
          originatingTraceRef.current = id
          setSelectedTraceId(id)
          window.requestAnimationFrame(() => {
            performance.measure('game-map-lazy-interaction-to-visible', interactionStart)
            performance.clearMarks(interactionStart)
          })
        } else {
          performance.clearMarks(interactionStart)
        }
      }
      instance.on('click', (event) => {
        selectActivityAtPoint(event.lngLat)
      })
      benchmarkInteraction = (event: Event) => {
        const point = (event as CustomEvent<{ lng: number; lat: number }>).detail
        if (point) selectActivityAtPoint(point)
      }
      if (benchmarkFullUpdate) {
        window.addEventListener('bikemapy:benchmark-map-click', benchmarkInteraction)
      }
      instance.on('mousemove', (event) => {
        const hit = nearestActivityId(
          mapInteractionActivitiesRef.current,
          visibleMemberIdsRef.current,
          event.lngLat,
          0.03 / 2 ** Math.max(0, instance.getZoom() - 8),
        )
        instance.getCanvas().style.cursor = hit ? 'pointer' : ''
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
      mapInteractionActivitiesRef.current = []
      visualDataRef.current = memberFeatureCollection([])
      drawVisualDataRef.current = () => undefined
      if (benchmarkFullUpdate && benchmarkInteraction) {
        window.removeEventListener('bikemapy:benchmark-map-click', benchmarkInteraction)
      }
      setMapDataIncludesAllMembers(false)
      if (mapLoadTimer.current !== null) window.clearTimeout(mapLoadTimer.current)
      mapLoadTimer.current = null
      instance.remove()
      map.current = null
    }
  }, [benchmarkFullUpdate, scheduleMapLoad])

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
      traceActivities.slice(0, traceListLimit).map((activity) => (
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
    [
      copy.gameMapDateUnknown,
      selectedCompetition,
      selectedTraceId,
      traceActivities,
      traceListLimit,
    ],
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
                onChange={(event) => {
                  clearMapData()
                  setCompetitionId(event.target.value)
                }}
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
                <section className="game-trace-list" aria-labelledby="game-trace-list-title">
                  <h2 id="game-trace-list-title">{copy.gameMapTraceList}</h2>
                  <div id="game-trace-viewport" ref={traceListRef} className="game-trace-viewport">
                    {traceButtons}
                  </div>
                  {traceListLimit < traceActivities.length && (
                    <button
                      type="button"
                      className="game-map-more-traces"
                      aria-controls="game-trace-viewport"
                      onClick={() => setTraceListLimit((limit) => limit + TRACE_PAGE_SIZE)}
                    >
                      {copy.gameMapShowMore}
                    </button>
                  )}
                </section>
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
