/* eslint-disable react-refresh/only-export-components -- map fixture helper is unit-tested. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AttributionControl, Map as MapLibreMap, NavigationControl } from 'maplibre-gl'
import type { GeoJSONSource } from 'maplibre-gl'

import { apiClient, rememberCsrfToken } from './api/client'
import type { components } from './api/generated/schema'
import { GameAccount } from './components/GameAccount'
import { MAP_PROVIDER } from './mapProvider'
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
      properties: { player_id: activity.player_id, calendar_date: activity.calendar_date, color: colors.get(activity.player_id) ?? '#2B8C76' },
      geometry: activity.geometry,
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
  const [competitionId, setCompetitionId] = useState<string | undefined>(() => readPrivateState().competitionId)
  const [visibleMembers, setVisibleMembers] = useState<number[] | null>(() => readPrivateState().members ?? null)
  const [mapData, setMapData] = useState<MapResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [mapLoading, setMapLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [signedOut, setSignedOut] = useState(false)
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null)
  const mapNode = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const bounds = useRef({ west: 14, south: 48.5, east: 19, north: 51.2, zoom: 7.5 })
  const loadMapRef = useRef<() => Promise<void>>(() => Promise.resolve())

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
    if (!competitionId || !map.current) return
    setMapLoading(true)
    setError(null)
    const current = bounds.current
    const selectedCompetition = competitions.find((competition) => competition.id === competitionId)
    const availableMembers = selectedCompetition?.members.map((member) => member.player_id) ?? []
    const selected = visibleMembers === null
      ? availableMembers
      : visibleMembers.filter((id) => availableMembers.includes(id))
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
            member: selected.length ? selected : [0],
          },
        },
        credentials: 'include',
      })
      if (result.response?.status === 401) {
        setSignedOut(true)
        return
      }
      if (result.response?.status === 404) throw new Error('map-not-found')
      if (!result.data) throw new Error('map-load')
      setMapData(result.data)
      const source = map.current?.getSource('private-traces') as GeoJSONSource | undefined
      const colors = new globalThis.Map(
        competitions.find((competition) => competition.id === competitionId)?.members.map(
          (member) => [member.player_id, member.color] as [number, string],
        ),
      )
      source?.setData(featureCollection(result.data.activities, colors))
    } catch {
      setError(copy.gameMapError)
    } finally {
      setMapLoading(false)
    }
  }, [competitionId, competitions, copy.gameMapError, visibleMembers])

  useEffect(() => {
    loadMapRef.current = loadMap
  }, [loadMap])

  useEffect(() => {
    if (!mapNode.current || map.current) return
    const instance = new MapLibreMap({
      container: mapNode.current,
      style: MAP_PROVIDER.style,
      center: [DEFAULT_VIEW.longitude, DEFAULT_VIEW.latitude] as [number, number],
      zoom: DEFAULT_VIEW.zoom,
      attributionControl: false,
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
      void loadMapRef.current()
    })
    instance.on('load', () => {
      instance.addSource('private-traces', { type: 'geojson', data: featureCollection([]) })
      instance.addLayer({
        id: 'private-traces',
        type: 'line',
        source: 'private-traces',
        paint: {
          'line-color': ['coalesce', ['get', 'color'], '#2B8C76'],
          'line-width': ['interpolate', ['linear'], ['zoom'], 5, 2, 14, 4],
          'line-opacity': 0.78,
        },
        layout: { 'line-cap': 'round', 'line-join': 'round' },
      })
      instance.on('click', 'private-traces', (event) => {
        const id = event.features?.[0]?.id
        if (id !== undefined) setSelectedTraceId(String(id))
      })
      instance.on('mouseenter', 'private-traces', () => {
        instance.getCanvas().style.cursor = 'pointer'
      })
      instance.on('mouseleave', 'private-traces', () => {
        instance.getCanvas().style.cursor = ''
      })
      void loadMapRef.current()
    })
    return () => {
      instance.remove()
      map.current = null
    }
  }, [])

  useEffect(() => {
    if (!competitionId) return
    const timer = window.setTimeout(() => void loadMap(), 0)
    return () => window.clearTimeout(timer)
  }, [competitionId, loadMap])

  useEffect(() => {
    writePrivateState(competitionId, visibleMembers ?? undefined)
  }, [competitionId, visibleMembers])

  const selectedCompetition = useMemo(
    () => competitions.find((competition) => competition.id === competitionId),
    [competitionId, competitions],
  )
  const selectedTrace = mapData?.activities.find((activity) => activity.id === selectedTraceId)
  const toggleMember = (id: number) =>
    setVisibleMembers((current) => {
      const members = current ?? selectedCompetition?.members.map((member) => member.player_id) ?? []
      return members.includes(id)
        ? members.filter((item) => item !== id)
        : [...members, id]
    })

  return (
    <main className="game-shell">
      <header className="game-topbar">
        <a className="wordmark" href="/" aria-label={copy.home}><span className="wordmark-mark" aria-hidden="true">↗</span><span>BikeMapy</span></a>
        <div className="game-topbar-actions">
          <button type="button" className="language-toggle" onClick={() => setLanguage((current) => (current === 'en' ? 'cs' : 'en'))} aria-label={copy.changeLanguage}>{language.toUpperCase()}</button>
          <GameAccount copy={copy} />
        </div>
      </header>
      <section className="game-map-layout" aria-labelledby="game-map-title">
        <aside className="game-map-controls">
          <span className="game-account-kicker">{copy.gameMapKicker}</span>
          <h1 id="game-map-title">{copy.gameMapTitle}</h1>
          <p>{copy.gameMapDescription}</p>
          {signedOut ? <div className="game-map-state"><strong>{copy.gameMapSignedOut}</strong><p>{copy.gameMapSignedOutDescription}</p></div> : loading ? <p role="status">{copy.gameMapLoading}</p> : error ? <div className="game-map-state"><p role="alert">{error}</p><button type="button" onClick={() => void loadCompetitions()}>{copy.gameMapRetry}</button></div> : competitions.length === 0 ? <p>{copy.gameMapNoCompetitions}</p> : <>
            <label className="game-map-label" htmlFor="game-competition">{copy.gameMapCompetition}</label>
            <select id="game-competition" value={competitionId ?? ''} onChange={(event) => setCompetitionId(event.target.value)}>
              {competitions.map((competition) => <option value={competition.id} key={competition.id}>{competition.name}</option>)}
            </select>
            <fieldset className="game-member-list"><legend>{copy.gameMapMembers}</legend>{selectedCompetition?.members.map((member) => <label key={member.player_id}><input type="checkbox" checked={visibleMembers === null || visibleMembers.includes(member.player_id)} onChange={() => toggleMember(member.player_id)} /><span className="member-swatch" style={{ backgroundColor: member.color }} aria-hidden="true" />{member.nickname || member.display_name}</label>)}</fieldset>
            {mapData && <p className="game-map-status" role="status">{statusMessage(mapData.status, copy)}{mapData.truncated ? ` ${copy.gameMapTruncated}` : ''}</p>}
          </>}
        </aside>
        <section className="game-map-stage" aria-label={copy.gameMapInteractive}>
          <div ref={mapNode} className="game-map-canvas" role="application" aria-label={copy.gameMapInteractive} />
          {mapLoading && <span className="game-map-loading" role="status">{copy.gameMapLoading}</span>}
          {selectedTrace && (
            <aside className="game-trace-detail" aria-label={copy.gameMapTraceDetail}>
              <button type="button" className="game-trace-close" onClick={() => setSelectedTraceId(null)} aria-label={copy.gameMapCloseTrace}>×</button>
              <span className="game-account-kicker">{copy.gameMapTraceDetail}</span>
              <strong>{copy.gameMapRideDate}</strong>
              <time dateTime={selectedTrace.calendar_date ?? undefined}>{selectedTrace.calendar_date ?? copy.gameMapDateUnknown}</time>
            </aside>
          )}
        </section>
      </section>
    </main>
  )
}
