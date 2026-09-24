import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  AttributionControl,
  Map as MapLibreMap,
  NavigationControl,
  setWorkerUrl,
} from 'maplibre-gl'
import type { GeoJSONSource, MapSourceDataEvent } from 'maplibre-gl'

import { apiClient, rememberCsrfToken } from '../api/client'
import type { components } from '../api/generated/schema'
import { MAP_PROVIDER } from '../mapProvider'
import { setMapLibreWorker } from '../maplibreWorker'
import type { Copy } from '../i18n/types'

type CaptureResponse = components['schemas']['CaptureResponse']
type CaptureFace = components['schemas']['CaptureFace']
type Competition = components['schemas']['Competition']

const DEFAULT_VIEW = { west: 14, south: 48.5, east: 19, north: 51.2, zoom: 7.5 }

function number(value: string) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 })
}

function signedNumber(value: string) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric === 0) return '0'
  return `${numeric > 0 ? '+' : ''}${numeric.toLocaleString(undefined, {
    maximumFractionDigits: 1,
  })}`
}

function latestMonthlyChange(member: CaptureResponse['members'][number]) {
  const value = member.monthly_net_change_m2.at(-1)?.net_change_m2
  return typeof value === 'string' ? value : undefined
}

function ownerColors(face: CaptureFace) {
  return face.owners.map((owner) => owner.color.toUpperCase()).sort()
}

function patternId(colors: string[]) {
  return `capture-hatch-${colors.join('-').replaceAll('#', '')}`
}

function addStripePattern(map: MapLibreMap, colors: string[]) {
  const id = patternId(colors)
  if (map.hasImage(id)) return id
  const canvas = document.createElement('canvas')
  canvas.width = 24
  canvas.height = 24
  const context = canvas.getContext('2d')
  if (!context) return id
  context.clearRect(0, 0, 24, 24)
  context.globalAlpha = 0.72
  colors.forEach((color, index) => {
    context.strokeStyle = color
    context.lineWidth = Math.max(3, 7 / colors.length)
    context.beginPath()
    const offset = (index * 24) / colors.length
    context.moveTo(offset - 24, 24)
    context.lineTo(offset + 24, -24)
    context.stroke()
  })
  map.addImage(id, context.getImageData(0, 0, 24, 24), { pixelRatio: 2 })
  return id
}

function captureFeatures(faces: CaptureFace[], map: MapLibreMap, visible: Set<number> | null) {
  return faces
    .filter((face) => visible === null || face.owners.some((owner) => visible.has(owner.player_id)))
    .map((face) => {
      const colors = ownerColors(face)
      return {
        type: 'Feature' as const,
        id: face.id,
        properties: {
          shared: face.shared,
          color: colors[0] ?? '#2B8C76',
          pattern: face.shared ? addStripePattern(map, colors) : '',
        },
        geometry: face.geometry as never,
      }
    })
}

export function CaptureDashboard({
  copy,
  competitions,
  competitionId,
  setCompetitionId,
  signedOut,
}: {
  copy: Copy
  competitions: Competition[]
  competitionId?: string
  setCompetitionId: (id: string) => void
  signedOut: boolean
}) {
  const [capture, setCapture] = useState<CaptureResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [errorCompetitionId, setErrorCompetitionId] = useState<string | undefined>()
  const [visibility, setVisibility] = useState<{
    competitionId?: string
    members: Set<number> | null
  }>({ members: null })
  const mapNode = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const mapLoaded = useRef(false)
  const loadCaptureRef = useRef<() => Promise<void>>(() => Promise.resolve())
  const requestSequence = useRef(0)
  const renderSequence = useRef(0)
  const bounds = useRef(DEFAULT_VIEW)

  const loadCapture = useCallback(async () => {
    if (!competitionId || !mapLoaded.current || !map.current) return
    const sequence = ++requestSequence.current
    setLoading(true)
    setError(null)
    const current = bounds.current
    const visibleIds = visibility.competitionId === competitionId ? visibility.members : null
    try {
      const result = await apiClient.GET('/api/v1/game/competitions/{competition_id}/capture/', {
        params: {
          path: { competition_id: competitionId },
          query: {
            west: current.west,
            south: current.south,
            east: current.east,
            north: current.north,
            zoom: Math.round(current.zoom),
            member: visibleIds === null ? undefined : visibleIds.size ? [...visibleIds] : [0],
          },
        },
        credentials: 'include',
      })
      rememberCsrfToken(result.response)
      if (sequence !== requestSequence.current) return
      if (result.response?.status === 401) return
      if (result.response?.status === 404 || !result.data) throw new Error('capture')
      if (result.data.competition_id !== competitionId) return
      setCapture(result.data)
    } catch {
      if (sequence === requestSequence.current) {
        setErrorCompetitionId(competitionId)
        setError(copy.gameCaptureError)
      }
    } finally {
      if (sequence === requestSequence.current) setLoading(false)
    }
  }, [competitionId, copy.gameCaptureError, visibility])

  useEffect(() => {
    loadCaptureRef.current = loadCapture
  }, [loadCapture])

  useLayoutEffect(() => {
    requestSequence.current += 1
    const source = map.current?.getSource('capture-territory') as GeoJSONSource | undefined
    source?.setData({ type: 'FeatureCollection', features: [] })
    mapNode.current?.removeAttribute('data-capture-response-loaded')
  }, [competitionId])

  useEffect(() => {
    if (!mapNode.current || map.current) return
    setMapLibreWorker(setWorkerUrl)
    const instance = new MapLibreMap({
      container: mapNode.current,
      style: MAP_PROVIDER.style,
      center: [16.6, 49.2],
      zoom: DEFAULT_VIEW.zoom,
      attributionControl: false,
      keyboard: true,
    })
    map.current = instance
    instance.addControl(new NavigationControl({ showCompass: true }), 'top-right')
    instance.addControl(new AttributionControl({ customAttribution: MAP_PROVIDER.attribution }))
    instance.on('load', () => {
      instance.addSource('capture-territory', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      instance.addLayer({
        id: 'capture-solid',
        type: 'fill',
        source: 'capture-territory',
        filter: ['!=', ['get', 'shared'], true],
        paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.48 },
      })
      instance.addLayer({
        id: 'capture-shared',
        type: 'fill',
        source: 'capture-territory',
        filter: ['==', ['get', 'shared'], true],
        paint: { 'fill-pattern': ['get', 'pattern'], 'fill-opacity': 0.68 },
      })
      mapLoaded.current = true
      void loadCaptureRef.current()
    })
    instance.on('moveend', () => {
      const current = instance.getBounds()
      bounds.current = {
        west: current.getWest(),
        south: current.getSouth(),
        east: current.getEast(),
        north: current.getNorth(),
        zoom: instance.getZoom(),
      }
      void loadCaptureRef.current()
    })
    return () => {
      mapLoaded.current = false
      instance.remove()
      map.current = null
    }
  }, [])

  useEffect(() => {
    if (mapLoaded.current) void loadCapture()
  }, [competitionId, loadCapture])

  const activeCapture = capture?.competition_id === competitionId ? capture : null
  const activeError = errorCompetitionId === competitionId ? error : null
  const visibleMembers = visibility.competitionId === competitionId ? visibility.members : null

  useEffect(() => {
    const activeMap = map.current
    const source = activeMap?.getSource('capture-territory') as GeoJSONSource | undefined
    if (!source || !activeMap || !activeCapture) return
    const mark = `capture-response-${renderSequence.current++}`
    performance.mark(mark)
    const onSourceData = (event: MapSourceDataEvent) => {
      if (event.sourceId !== 'capture-territory' || !event.isSourceLoaded) return
      activeMap.off('sourcedata', onSourceData)
      performance.measure('capture-response-to-render', mark)
      performance.clearMarks(mark)
    }
    activeMap.on('sourcedata', onSourceData)
    source.setData({
      type: 'FeatureCollection',
      features: captureFeatures(activeCapture.faces, activeMap, visibleMembers),
    })
    mapNode.current?.setAttribute('data-capture-response-loaded', 'true')
  }, [activeCapture, visibleMembers])

  const selectedMembers = useMemo(() => activeCapture?.members ?? [], [activeCapture])
  const toggleMember = (playerId: number) => {
    setVisibility((currentState) => {
      const current = currentState.competitionId === competitionId ? currentState.members : null
      const next = new Set(
        current ?? activeCapture?.members.map((member) => member.player_id) ?? [],
      )
      if (next.has(playerId)) next.delete(playerId)
      else next.add(playerId)
      return { competitionId, members: next }
    })
  }

  if (signedOut)
    return (
      <div className="capture-empty">
        <strong>{copy.gameMapSignedOut}</strong>
        <p>{copy.gameMapSignedOutDescription}</p>
      </div>
    )

  return (
    <section className="capture-layout" aria-labelledby="game-capture-title">
      <aside className="capture-ledger">
        <div className="capture-header">
          <span className="game-account-kicker">{copy.gameCaptureKicker}</span>
          <h1 id="game-capture-title">{copy.gameCaptureTitle}</h1>
          <p>{copy.gameCaptureDescription}</p>
          {competitions.length > 0 && (
            <label className="game-map-label" htmlFor="capture-competition">
              {copy.gameCaptureCompetition}
              <select
                id="capture-competition"
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
        {activeCapture && activeCapture.status !== 'fresh' && activeCapture.status !== 'empty' && (
          <p className={`capture-state capture-state-${activeCapture.status}`} role="status">
            {activeCapture.status === 'pending' ? copy.gameCapturePending : copy.gameCaptureFailed}
          </p>
        )}
        {activeCapture?.truncated && (
          <p className="capture-state capture-state-truncated" role="status">
            {copy.gameCaptureTruncated}
          </p>
        )}
        {activeError && (
          <div className="capture-state capture-state-failed" role="alert">
            <p>{activeError}</p>
            <button type="button" onClick={() => void loadCapture()}>
              {copy.gameCaptureRetry}
            </button>
          </div>
        )}
        <fieldset className="capture-members">
          <legend>{copy.gameCaptureMembers}</legend>
          {activeCapture?.members.map((member) => (
            <label key={member.player_id}>
              <input
                type="checkbox"
                checked={visibleMembers === null || visibleMembers.has(member.player_id)}
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
        <div className="capture-ranking" aria-label={copy.gameCaptureLeaderboard}>
          <h2>{copy.gameCaptureLeaderboard}</h2>
          {loading && <p role="status">{copy.gameCaptureLoading}</p>}
          {!loading && !activeError && !activeCapture?.members.length && (
            <p>{copy.gameCaptureEmpty}</p>
          )}
          {selectedMembers.map((member) => (
            <div className="capture-ranking-row" key={member.player_id}>
              <span className="capture-rank">{member.rank}</span>
              <span
                className="member-swatch"
                style={{ backgroundColor: member.color }}
                aria-hidden="true"
              />
              <span className="capture-rider">
                <strong>{member.nickname || member.display_name}</strong>
                {latestMonthlyChange(member) && (
                  <small>
                    {copy.gameCaptureMonthly}: {signedNumber(latestMonthlyChange(member) ?? '0')} m²
                  </small>
                )}
              </span>
              <strong className="capture-area">{number(member.area_m2)} m²</strong>
            </div>
          ))}
        </div>
        <details className="capture-help">
          <summary>{copy.gameCaptureHelp}</summary>
          <ul>
            <li>{copy.gameCaptureConnectionRule}</li>
            <li>{copy.gameCaptureBoundaryPriority}</li>
            <li>{copy.gameCaptureSameDay}</li>
          </ul>
        </details>
      </aside>
      <section className="capture-map-stage" aria-label={copy.gameMapInteractive}>
        <div
          ref={mapNode}
          className="game-map-canvas"
          role="application"
          aria-label={copy.gameMapInteractive}
        />
        {activeCapture?.is_final && (
          <span className="capture-map-status">{copy.gameCaptureFinal}</span>
        )}
        {activeCapture && !activeCapture.is_final && (
          <span className="capture-map-status">{copy.gameCaptureNotFinal}</span>
        )}
        <span className="capture-map-legend">
          <i className="capture-legend-solid" /> {copy.gameCaptureArea}
          <i className="capture-legend-hatch" /> {copy.gameCaptureShared}
        </span>
      </section>
    </section>
  )
}
