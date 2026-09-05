/* eslint-disable react-refresh/only-export-components -- state helpers are exported for interaction tests. */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import type { GeoJSONSource, Map as MapLibreMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import type { components } from './api/generated/schema'
import { apiClient } from './api/client'
import { DEFAULT_VIEW, MAP_PROVIDER } from './mapProvider'
import { normalizePublicSiteUrl, routeUrl } from './siteMetadata'

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

type Language = 'en' | 'cs'

const LANGUAGE_KEY = 'bikemapy:language'
const PUBLIC_SITE_URL = normalizePublicSiteUrl(import.meta.env.VITE_PUBLIC_SITE_URL)

const translations = {
  en: {
    siteTitle: 'BikeMapy — routes with a source',
    siteDescription: 'Discover cycling routes shared in BikeForum discussions.',
    home: 'BikeMapy home',
    indexed: (count: number) => (count ? `${count} rides indexed` : 'Open ride archive'),
    changeLanguage: 'Change language',
    map: 'Route map',
    interactiveMap: 'Interactive route map',
    mapLoading: 'Updating this view…',
    mapUnavailable: 'The map data could not be loaded',
    mapStartError: 'The interactive map could not start.',
    mapTilesError: 'Map tiles are unavailable. You can retry the map.',
    mapPaused: 'Map data paused',
    retryMap: 'Retry map',
    tryAgain: 'Try again',
    filteredDensity: 'Filtered route density',
    density: 'Route density',
    routesInView: (count: number) => `${count} routes in view`,
    selected: 'selected',
    nearby: 'nearby',
    hideRoutes: 'Hide routes',
    showRoutes: 'Show routes',
    searchRoutes: 'Search routes',
    eyebrow: 'A map with a memory',
    heading: 'Find the ride',
    headingSecond: 'worth repeating.',
    intro:
      'Explore the roads people return to. Search the archive, then let the map take the lead.',
    searchPlaceholder: 'Search places, authors, routes',
    author: 'Author',
    authorPlaceholder: 'e.g. Jana',
    category: 'Category',
    categoryPlaceholder: 'e.g. gravel',
    distanceFrom: 'Distance from (m)',
    distanceTo: 'Distance to (m)',
    climbFrom: 'Climb from (m)',
    climbTo: 'Climb to (m)',
    reading: 'Reading the archive…',
    unavailable: 'Archive unavailable',
    ridesFound: (count: number) => `${count} rides found`,
    clearFilters: 'Clear filters',
    currentViewport: 'Current viewport',
    viewportHint: 'List follows the current map bounds',
    lostConnection: 'We lost the archive connection.',
    checkApi: 'Check the API and try again.',
    retry: 'Retry',
    noMatches: 'No rides match yet.',
    widerSearch: 'Try a wider search or clear the filters.',
    routes: 'Routes',
    uncategorised: 'Uncategorised',
    distanceUnknown: 'Distance unknown',
    selectedRoute: 'Selected route',
    previousOverlap: 'Previous overlapping route',
    nextOverlap: 'Next overlapping route',
    loadingRoute: 'Loading route…',
    detailsUnavailable: 'Route details are unavailable',
    geometryUnavailable: 'Full route geometry is unavailable right now.',
    distance: 'Distance',
    ascent: 'Ascent',
    descent: 'Descent',
    elevation: 'Elevation',
    elevationProfile: 'Elevation profile',
    loop: 'Loop',
    pointToPoint: 'Point to point',
    routeType: 'Route type',
    sourceStatus: 'Source status',
    verifiedSource: 'Verified source',
    communitySource: 'Community source',
    unavailableSource: 'Unavailable source',
    unknownSource: 'Source status unknown',
    fullGeometryLoading: 'Loading full geometry…',
    geometryFramed: 'Full geometry framed on map',
    detailsStillLoading: 'Route details are still loading…',
    retryDetails: 'Retry route details',
    retryGeometry: 'Retry geometry',
    closeDetails: 'Close route details',
    sources: 'BikeForum sources',
    sourceLink: (title: string) => `Open source ${title}`,
    mapyLink: 'Open Mapy.com route',
    noSources: 'No public forum sources are available.',
    sourceChecked: (date: string) => `Last checked ${date}`,
    sourceLastSuccessfulCheck: (date: string) => `Last successful check ${date}`,
    posted: (date: string) => `Posted ${date}`,
    reviewed: 'Reviewed',
    reviewedDisclaimer:
      'Review covers catalogue criteria only. It does not indicate safety, passability, or legal access.',
    share: 'Share route',
    copyLink: 'Copy permanent link',
    copied: 'Link copied',
    copyFailed: 'Copy unavailable — use the address bar.',
    report: 'Report a problem',
    reportTitle: 'Report a problem with this route',
    reportLabel: 'What should we check?',
    reportPlaceholder: 'Describe an issue with the route or its attribution…',
    sendReport: 'Report unavailable',
    reportThanks: 'Secure reporting is not available yet. No report was submitted.',
    cancel: 'Cancel',
    gpxDownload: 'Download GPX',
    gpxUnavailable: 'GPX download is unavailable until redistribution is legally approved.',
  },
  cs: {
    siteTitle: 'BikeMapy — trasy se zdrojem',
    siteDescription: 'Objevujte cyklistické trasy sdílené v diskusích BikeFora.',
    home: 'Domů BikeMapy',
    indexed: (count: number) => (count ? `${count} tras v archivu` : 'Otevřený archiv tras'),
    changeLanguage: 'Změnit jazyk',
    map: 'Mapa tras',
    interactiveMap: 'Interaktivní mapa tras',
    mapLoading: 'Aktualizuji toto zobrazení…',
    mapUnavailable: 'Data mapy se nepodařilo načíst',
    mapStartError: 'Interaktivní mapu se nepodařilo spustit.',
    mapTilesError: 'Dlaždice mapy nejsou dostupné. Zkuste mapu načíst znovu.',
    mapPaused: 'Data mapy pozastavena',
    retryMap: 'Načíst mapu znovu',
    tryAgain: 'Zkusit znovu',
    filteredDensity: 'Hustota filtrovaných tras',
    density: 'Hustota tras',
    routesInView: (count: number) => `${count} tras v zobrazení`,
    selected: 'vybraná',
    nearby: 'okolní',
    hideRoutes: 'Skrýt trasy',
    showRoutes: 'Zobrazit trasy',
    searchRoutes: 'Hledat trasy',
    eyebrow: 'Mapa s pamětí',
    heading: 'Najděte trasu',
    headingSecond: 'ke které se vrátíte.',
    intro: 'Prozkoumejte cesty, na které se lidé vracejí. Prohledejte archiv a nechte vést mapu.',
    searchPlaceholder: 'Místa, autoři, trasy',
    author: 'Autor',
    authorPlaceholder: 'např. Jana',
    category: 'Kategorie',
    categoryPlaceholder: 'např. gravel',
    distanceFrom: 'Vzdálenost od (m)',
    distanceTo: 'Vzdálenost do (m)',
    climbFrom: 'Stoupání od (m)',
    climbTo: 'Stoupání do (m)',
    reading: 'Procházím archiv…',
    unavailable: 'Archiv není dostupný',
    ridesFound: (count: number) => `${count} tras nalezeno`,
    clearFilters: 'Zrušit filtry',
    currentViewport: 'Aktuální výřez',
    viewportHint: 'Seznam sleduje hranice mapy',
    lostConnection: 'Spojení s archivem se přerušilo.',
    checkApi: 'Zkontrolujte API a zkuste to znovu.',
    retry: 'Zkusit znovu',
    noMatches: 'Žádná trasa neodpovídá.',
    widerSearch: 'Zkuste širší hledání nebo zrušte filtry.',
    routes: 'Trasy',
    uncategorised: 'Bez kategorie',
    distanceUnknown: 'Vzdálenost neznámá',
    selectedRoute: 'Vybraná trasa',
    previousOverlap: 'Předchozí překrývající se trasa',
    nextOverlap: 'Další překrývající se trasa',
    loadingRoute: 'Načítám trasu…',
    detailsUnavailable: 'Podrobnosti trasy nejsou dostupné',
    geometryUnavailable: 'Celá geometrie trasy není nyní dostupná.',
    distance: 'Vzdálenost',
    ascent: 'Stoupání',
    descent: 'Klesání',
    elevation: 'Výškové údaje',
    elevationProfile: 'Výškový profil',
    loop: 'Okruh',
    pointToPoint: 'Z bodu do bodu',
    routeType: 'Typ trasy',
    sourceStatus: 'Stav zdroje',
    verifiedSource: 'Ověřený zdroj',
    communitySource: 'Komunitní zdroj',
    unavailableSource: 'Nedostupný zdroj',
    unknownSource: 'Stav zdroje neznámý',
    fullGeometryLoading: 'Načítám celou geometrii…',
    geometryFramed: 'Celá geometrie zobrazena na mapě',
    detailsStillLoading: 'Podrobnosti trasy se stále načítají…',
    retryDetails: 'Načíst podrobnosti znovu',
    retryGeometry: 'Načíst geometrii znovu',
    closeDetails: 'Zavřít podrobnosti trasy',
    sources: 'Zdroje z BikeFora',
    sourceLink: (title: string) => `Otevřít zdroj ${title}`,
    mapyLink: 'Otevřít trasu na Mapy.com',
    noSources: 'Veřejné zdroje z fóra nejsou k dispozici.',
    sourceChecked: (date: string) => `Naposledy ověřeno ${date}`,
    sourceLastSuccessfulCheck: (date: string) => `Poslední úspěšná kontrola ${date}`,
    posted: (date: string) => `Publikováno ${date}`,
    reviewed: 'Prověřeno',
    reviewedDisclaimer:
      'Prověření se týká pouze katalogových kritérií. Neoznačuje bezpečnost, průchodnost ani právní přístup.',
    share: 'Sdílet trasu',
    copyLink: 'Kopírovat trvalý odkaz',
    copied: 'Odkaz zkopírován',
    copyFailed: 'Kopírování není dostupné — použijte adresní řádek.',
    report: 'Nahlásit problém',
    reportTitle: 'Nahlásit problém s trasou',
    reportLabel: 'Co máme prověřit?',
    reportPlaceholder: 'Popište problém s trasou nebo uvedením zdroje…',
    sendReport: 'Hlášení není dostupné',
    reportThanks: 'Bezpečné hlášení zatím není dostupné. Hlášení nebylo odesláno.',
    cancel: 'Zrušit',
    gpxDownload: 'Stáhnout GPX',
    gpxUnavailable: 'Stažení GPX není dostupné, dokud nebude právně schváleno další šíření.',
  },
} as const
type Copy = (typeof translations)[Language]

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
  routeSlug?: string | null,
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
  if (routeId && routeSlug) params.set('slug', routeSlug)
  if (viewportOnly) params.set('inview', '1')
  const query = params.toString()
  const url = `${window.location.pathname}${query ? `?${query}` : ''}`
  window.history[mode === 'push' ? 'pushState' : 'replaceState'](
    { filters, view, routeId },
    '',
    url,
  )
}

function initialLanguage(): Language {
  try {
    const saved = localStorage.getItem(LANGUAGE_KEY)
    if (saved === 'en' || saved === 'cs') return saved
  } catch {
    /* private browsing */
  }
  return navigator.language.toLowerCase().startsWith('cs') ? 'cs' : 'en'
}

function formatMetric(value: string | null | undefined, suffix: string): string {
  if (!value) return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return `${Math.round(number)} ${suffix}`
}

function hasMetric(value: string | null | undefined): value is string {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
}

type ElevationPoint = { distance_m: number; elevation_m: number }

function ElevationProfile({ points, copy }: { points: ElevationPoint[]; copy: Copy }) {
  if (!points.length) return null
  const minimum = Math.min(...points.map((point) => point.elevation_m))
  const maximum = Math.max(...points.map((point) => point.elevation_m))
  const distance = Math.max(points.at(-1)?.distance_m ?? 0, 1)
  const range = Math.max(maximum - minimum, 1)
  const coordinates = points
    .map((point) => {
      const x = (point.distance_m / distance) * 100
      const y = 96 - ((point.elevation_m - minimum) / range) * 86
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
  const formatDistance = (value: number) => `${(value / 1000).toFixed(1)} km`
  const formatElevation = (value: number) => `${Math.round(value)} m`
  return (
    <figure className="elevation-profile" aria-labelledby="elevation-profile-title">
      <figcaption id="elevation-profile-title">{copy.elevationProfile}</figcaption>
      <svg viewBox="0 0 100 100" role="img" aria-labelledby="elevation-profile-title">
        <polyline points={coordinates} />
      </svg>
      <ol aria-label={copy.elevationProfile}>
        {points.map((point, index) => (
          <li key={`${point.distance_m}-${point.elevation_m}-${index}`}>
            <span>{formatDistance(point.distance_m)}</span>
            <span>{formatElevation(point.elevation_m)}</span>
          </li>
        ))}
      </ol>
    </figure>
  )
}

function routeStatus(status: string, copy: Copy): string {
  if (status === 'verified') return copy.verifiedSource
  if (status === 'available' || status === 'community') return copy.communitySource
  if (status === 'unavailable') return copy.unavailableSource
  return copy.unknownSource
}

function setMeta(name: string, content: string, property = false) {
  const attribute = property ? 'property' : 'name'
  let element = document.head.querySelector<HTMLMetaElement>(`meta[${attribute}="${name}"]`)
  if (!element) {
    element = document.createElement('meta')
    element.setAttribute(attribute, name)
    document.head.appendChild(element)
  }
  element.content = content
}

function setCanonical(href: string) {
  let link = document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]')
  if (!link) {
    link = document.createElement('link')
    link.rel = 'canonical'
    document.head.appendChild(link)
  }
  link.href = href
}

function useRouteList(
  filters: Filters,
  view: ViewState,
  viewportOnly: boolean,
  retryToken: number,
  copy: Copy,
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
        if (error || !data) throw new Error(copy.unavailable)
        setState({ routes: data.results, count: data.count, loading: false, error: null })
      })
      .catch((error: unknown) => {
        if (active)
          setState((current) => ({
            ...current,
            loading: false,
            error: error instanceof Error ? error.message : copy.unavailable,
          }))
      })
    return () => {
      active = false
    }
  }, [copy.unavailable, query, retryToken])
  return state
}

function useViewport(
  view: ViewState,
  filters: Filters,
  ready: boolean,
  retryToken: number,
  copy: Copy,
) {
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
        if (error || !data) throw new Error(copy.mapUnavailable)
        setState({ data, loading: false, error: null })
      })
      .catch((error: unknown) => {
        if (active)
          setState((current) => ({
            ...current,
            loading: false,
            error: error instanceof Error ? error.message : copy.mapUnavailable,
          }))
      })
    return () => {
      active = false
    }
  }, [
    copy.mapUnavailable,
    filters,
    ready,
    retryToken,
    view.bounds,
    view.latitude,
    view.longitude,
    view.zoom,
  ])
  return state
}

function App() {
  const initial = useMemo(parseState, [])
  const [language, setLanguage] = useState<Language>(initialLanguage)
  const copy = translations[language]
  const copyRef = useRef(copy)
  copyRef.current = copy
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
  const [reportOpen, setReportOpen] = useState(false)
  const [reportSent, setReportSent] = useState(false)
  const [shareState, setShareState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [mobilePanelHeight, setMobilePanelHeight] = useState<number | null>(null)
  const [mobileDetailHeight, setMobileDetailHeight] = useState<number | null>(null)
  const cameraSyncRef = useRef(false)
  const mapNode = useRef<HTMLDivElement>(null)
  const panelNode = useRef<HTMLElement>(null)
  const detailNode = useRef<HTMLElement>(null)
  const reportMessageNode = useRef<HTMLTextAreaElement>(null)
  const reportTriggerNode = useRef<HTMLButtonElement>(null)
  const reportDialogNode = useRef<HTMLElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const { routes, count, loading, error } = useRouteList(
    filters,
    view,
    viewportOnly,
    retryToken,
    copy,
  )
  const viewport = useViewport(view, filters, mapReady, retryToken, copy)
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
  const permanentRouteUrl = selectedRoute
    ? routeUrl(PUBLIC_SITE_URL, selectedRoute.id, selectedRoute.slug)
    : ''

  useEffect(() => {
    if (!reportOpen) return
    const previous = document.activeElement as HTMLElement | null
    const trigger = reportTriggerNode.current
    const focusReport = () => reportMessageNode.current?.focus()
    focusReport()
    const shell = document.querySelector('.app-shell')
    const background = shell
      ? Array.from(shell.children).filter((node) => !node.classList.contains('report-backdrop'))
      : []
    background.forEach((node) => node.setAttribute('inert', ''))
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setReportOpen(false)
    }
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !reportDialogNode.current) return
      const focusable = Array.from(
        reportDialogNode.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), textarea, a[href], input:not([disabled]), select:not([disabled])',
        ),
      )
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', closeOnEscape)
    document.addEventListener('keydown', trapFocus)
    return () => {
      document.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('keydown', trapFocus)
      background.forEach((node) => node.removeAttribute('inert'))
      ;(previous ?? trigger)?.focus()
    }
  }, [reportOpen])

  useEffect(() => {
    try {
      localStorage.setItem(LANGUAGE_KEY, language)
    } catch {
      /* private browsing */
    }
    document.documentElement.lang = language
    const title = selectedRoute ? `${selectedRoute.title} · BikeMapy` : copy.siteTitle
    const description = selectedRoute
      ? `${selectedRoute.title}. ${copy.intro}`
      : copy.siteDescription
    document.title = title
    setMeta('description', description)
    setMeta('og:title', title, true)
    setMeta('og:description', description, true)
    setMeta('og:type', selectedRoute ? 'article' : 'website', true)
    setMeta('og:site_name', 'BikeMapy', true)
    setMeta('og:locale', language === 'cs' ? 'cs_CZ' : 'en_US', true)
    setMeta('og:url', permanentRouteUrl || `${PUBLIC_SITE_URL}/`, true)
    setCanonical(permanentRouteUrl || `${PUBLIC_SITE_URL}/`)
  }, [copy.intro, copy.siteDescription, copy.siteTitle, language, permanentRouteUrl, selectedRoute])
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
          setSelectedMetadataError(copy.detailsUnavailable)
        } else {
          setSelectedRecord(data)
          setSelectedMetadataError(null)
        }
        setSelectedMetadataLoading(false)
      })
      .catch(() => {
        if (active) {
          setSelectedMetadataError(copy.detailsUnavailable)
          setSelectedMetadataLoading(false)
        }
      })
    return () => {
      active = false
    }
  }, [copy.detailsUnavailable, metadataRetryToken, retryToken, selectedId])
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
            setSelectedGeometryError(copy.geometryUnavailable)
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
          setSelectedGeometryError(copy.geometryUnavailable)
          setSelectedGeometryLoading(false)
        }
      })
    return () => {
      active = false
    }
  }, [copy.geometryUnavailable, geometryRetryToken, retryToken, selectedId])
  useEffect(() => {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ filters, routeId: selectedId, viewportOnly }),
      )
    } catch {
      /* private browsing */
    }
    writeUrl(filters, view, selectedId, 'replace', viewportOnly, selectedRoute?.slug)
  }, [filters, selectedRoute?.slug, selectedId, view, viewportOnly])
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
          setMapError(copyRef.current.mapStartError)
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
          if (event.error) setMapError(copyRef.current.mapTilesError)
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
      .catch(() => setMapError(copyRef.current.mapStartError))
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
        const reducedMotion =
          window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
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
          duration: reducedMotion ? 0 : 600,
        })
      }
    }
  }, [mapReady, mobileDetailHeight, mobilePanelHeight, panelOpen, selected])

  const updateFilter = (key: keyof Filters, value: string) =>
    setFilters((current) => ({ ...current, [key]: value }))
  const resetFilters = () => setFilters(DEFAULT_FILTERS)
  const retry = () => setRetryToken((current) => current + 1)
  const copyPermanentLink = async () => {
    if (!permanentRouteUrl) return
    try {
      await navigator.clipboard.writeText(permanentRouteUrl)
      setShareState('copied')
    } catch {
      setShareState('failed')
    }
  }
  const toggleLanguage = () => setLanguage((current) => (current === 'en' ? 'cs' : 'en'))
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
      <a className="skip-link" href="#route-browser">
        {copy.routes}
      </a>
      <header className="topbar">
        <a className="wordmark" href="/" aria-label={copy.home}>
          <span className="wordmark-mark" aria-hidden="true">
            ↗
          </span>
          <span>BikeMapy</span>
        </a>
        <div className="topbar-meta">
          <span className="live-indicator">
            <i aria-hidden="true" /> {copy.indexed(count)}
          </span>
          <button
            type="button"
            className="language-switcher"
            aria-label={copy.changeLanguage}
            aria-pressed={language === 'cs'}
            onClick={toggleLanguage}
          >
            {language === 'en' ? 'EN / CZ' : 'CZ / EN'}
          </button>
        </div>
      </header>
      <section className="map-stage" aria-label={copy.map}>
        <div
          ref={mapNode}
          className="map-canvas"
          role="application"
          aria-label={copy.interactiveMap}
        />
        <div className="map-vignette" aria-hidden="true" />
        <div className="map-status" role="status">
          {viewport.loading
            ? copy.mapLoading
            : viewport.error
              ? viewport.error
              : Object.values(filters).some(Boolean) && viewport.data?.mode === 'heatmap'
                ? copy.filteredDensity
                : viewport.data?.mode === 'heatmap'
                  ? copy.density
                  : copy.routesInView(viewport.data?.routes.length ?? 0)}
        </div>
        <div className="map-empty">
          {(mapError || viewport.error) && (
            <>
              <strong>{mapError ?? copy.mapPaused}</strong>
              <button
                type="button"
                onClick={mapError ? () => setMapRetry((current) => current + 1) : retry}
              >
                {mapError ? copy.retryMap : copy.tryAgain}
              </button>
            </>
          )}
        </div>
        <div className="map-legend">
          <span className="legend-line" /> {copy.selected} <span className="legend-muted" />{' '}
          {copy.nearby}
        </div>
      </section>
      <aside
        ref={panelNode}
        className={`route-panel ${panelOpen ? 'is-open' : 'is-collapsed'}`}
        aria-label={copy.searchRoutes}
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
          {panelOpen ? copy.hideRoutes : copy.showRoutes}{' '}
          <span aria-hidden="true">{panelOpen ? '−' : '+'}</span>
        </button>
        <div id="route-browser" className="panel-content">
          <p className="eyebrow">{copy.eyebrow}</p>
          <h1>
            {copy.heading}
            <br />
            {copy.headingSecond}
          </h1>
          <p className="intro">{copy.intro}</p>
          <label className="search-field">
            <span aria-hidden="true">⌕</span>
            <span className="sr-only">{copy.searchRoutes}</span>
            <input
              type="search"
              aria-label={copy.searchRoutes}
              value={filters.search}
              onChange={(event) => updateFilter('search', event.target.value)}
              placeholder={copy.searchPlaceholder}
            />
          </label>
          <div className="filter-grid">
            <label>
              {copy.author}
              <input
                aria-label={copy.author}
                value={filters.author}
                onChange={(event) => updateFilter('author', event.target.value)}
                placeholder={copy.authorPlaceholder}
              />
            </label>
            <label>
              {copy.category}
              <input
                aria-label={copy.category}
                value={filters.category}
                onChange={(event) => updateFilter('category', event.target.value)}
                placeholder={copy.categoryPlaceholder}
              />
            </label>
            <label>
              {copy.distanceFrom}
              <input
                aria-label={copy.distanceFrom}
                inputMode="numeric"
                value={filters.min_distance_m}
                onChange={(event) => updateFilter('min_distance_m', event.target.value)}
              />
            </label>
            <label>
              {copy.distanceTo}
              <input
                aria-label={copy.distanceTo}
                inputMode="numeric"
                value={filters.max_distance_m}
                onChange={(event) => updateFilter('max_distance_m', event.target.value)}
              />
            </label>
            <label>
              {copy.climbFrom}
              <input
                aria-label={copy.climbFrom}
                inputMode="numeric"
                value={filters.min_ascent_m}
                onChange={(event) => updateFilter('min_ascent_m', event.target.value)}
              />
            </label>
            <label>
              {copy.climbTo}
              <input
                aria-label={copy.climbTo}
                inputMode="numeric"
                value={filters.max_ascent_m}
                onChange={(event) => updateFilter('max_ascent_m', event.target.value)}
              />
            </label>
          </div>
          <div className="results-heading">
            <span>
              {loading
                ? copy.reading
                : error
                  ? copy.unavailable
                  : copy.ridesFound(displayRoutes.length)}
            </span>
            <button
              type="button"
              onClick={resetFilters}
              disabled={!Object.values(filters).some(Boolean)}
            >
              {copy.clearFilters}
            </button>
          </div>
          <label className="viewport-filter">
            <input
              type="checkbox"
              checked={viewportOnly}
              onChange={(event) => setViewportOnly(event.target.checked)}
            />
            <span>{copy.currentViewport}</span>
            {viewportOnly && viewport.data?.mode === 'heatmap' && (
              <small>{copy.viewportHint}</small>
            )}
          </label>
          {error && (
            <div className="notice error-notice">
              <strong>{copy.lostConnection}</strong>
              <span>{copy.checkApi}</span>
              <button type="button" onClick={retry}>
                {copy.retry}
              </button>
            </div>
          )}
          {!loading && !error && displayRoutes.length === 0 && (
            <div className="notice">
              <strong>{copy.noMatches}</strong>
              <span>{copy.widerSearch}</span>
              <button type="button" onClick={resetFilters}>
                {copy.clearFilters}
              </button>
            </div>
          )}
          <nav className="route-list" aria-label={copy.routes}>
            {displayRoutes.map((route) => (
              <button
                key={route.id}
                type="button"
                className={`route-card ${route.id === selectedId ? 'is-selected' : ''}`}
                onClick={() => selectRoute(route.id)}
              >
                <span className="route-card-title">{route.title}</span>
                {(route.categories[0] || hasMetric(route.distance_m)) && (
                  <span className="route-card-meta">
                    {route.categories[0] && <span>{route.categories[0].name}</span>}
                    {hasMetric(route.distance_m) && (
                      <span>{(Number(route.distance_m) / 1000).toFixed(1)} km</span>
                    )}
                  </span>
                )}
              </button>
            ))}
          </nav>
        </div>
      </aside>
      {selectionVisible && (
        <section ref={detailNode} className="route-detail" aria-labelledby="route-detail-title">
          <div className="detail-kicker">
            {copy.selectedRoute}{' '}
            {overlap.length > 1 && (
              <span className="overlap-picker">
                <button
                  type="button"
                  onClick={() => nextOverlap(-1)}
                  aria-label={copy.previousOverlap}
                >
                  ‹
                </button>
                <span>
                  {Math.max(1, selectedIndex + 1)} / {overlap.length}
                </span>
                <button type="button" onClick={() => nextOverlap(1)} aria-label={copy.nextOverlap}>
                  ›
                </button>
              </span>
            )}
          </div>
          <h2 id="route-detail-title">
            {selectedRoute?.title ??
              (selectedMetadataLoading ? copy.loadingRoute : copy.detailsUnavailable)}
          </h2>
          {selectedRoute && (
            <>
              {selectedRoute.reviewed && (
                <div className="reviewed-badge" title={copy.reviewedDisclaimer}>
                  {copy.reviewed}
                </div>
              )}
              {(hasMetric(selectedRoute.distance_m) ||
                hasMetric(selectedRoute.ascent_m) ||
                hasMetric(selectedRoute.descent_m) ||
                selectedRoute.loop_status === 'loop' ||
                selectedRoute.loop_status === 'point_to_point') && (
                <dl className="detail-stats">
                  {hasMetric(selectedRoute.distance_m) && (
                    <div>
                      <dt>{copy.distance}</dt>
                      <dd>{formatMetric(selectedRoute.distance_m, 'm')}</dd>
                    </div>
                  )}
                  {hasMetric(selectedRoute.ascent_m) && (
                    <div>
                      <dt>{copy.ascent}</dt>
                      <dd>{formatMetric(selectedRoute.ascent_m, 'm')}</dd>
                    </div>
                  )}
                  {hasMetric(selectedRoute.descent_m) && (
                    <div>
                      <dt>{copy.descent}</dt>
                      <dd>{formatMetric(selectedRoute.descent_m, 'm')}</dd>
                    </div>
                  )}
                  {(selectedRoute.loop_status === 'loop' ||
                    selectedRoute.loop_status === 'point_to_point') && (
                    <div>
                      <dt>{copy.routeType}</dt>
                      <dd>
                        {selectedRoute.loop_status === 'loop' ? copy.loop : copy.pointToPoint}
                      </dd>
                    </div>
                  )}
                </dl>
              )}
              {selectedRoute.elevation_profile && (
                <ElevationProfile points={selectedRoute.elevation_profile} copy={copy} />
              )}
              {selectedRoute.categories.length > 0 && (
                <ul className="detail-categories" aria-label={copy.category}>
                  {selectedRoute.categories.map((category) => (
                    <li key={category.slug}>{category.name}</li>
                  ))}
                </ul>
              )}
              {selectedRoute.reviewed && (
                <p className="reviewed-disclaimer">{copy.reviewedDisclaimer}</p>
              )}
            </>
          )}
          <p className="detail-source">
            {selectedMetadataError
              ? null
              : (selectedGeometryError ??
                (selectedRoute
                  ? `${routeStatus(selectedRoute.source_status, copy)} · ${selectedGeometryLoading ? copy.fullGeometryLoading : copy.geometryFramed}`
                  : copy.detailsStillLoading))}
          </p>
          {selectedMetadataError && (
            <button
              type="button"
              className="detail-retry"
              onClick={() => setMetadataRetryToken((current) => current + 1)}
            >
              {copy.retryDetails}
            </button>
          )}
          {selectedGeometryError && (
            <button
              type="button"
              className="detail-retry"
              onClick={() => setGeometryRetryToken((current) => current + 1)}
            >
              {copy.retryGeometry}
            </button>
          )}
          {selectedRoute && (
            <>
              <div className="detail-actions" aria-label={copy.share}>
                <a className="detail-link" href={permanentRouteUrl}>
                  {copy.share}
                </a>
                <button type="button" className="detail-link" onClick={copyPermanentLink}>
                  {shareState === 'copied'
                    ? copy.copied
                    : shareState === 'failed'
                      ? copy.copyFailed
                      : copy.copyLink}
                </button>
                <button
                  type="button"
                  className="detail-link detail-report"
                  ref={reportTriggerNode}
                  onClick={() => {
                    setReportSent(false)
                    setReportOpen(true)
                  }}
                >
                  {copy.report}
                </button>
                {selectedRoute.gpx_download_url ? (
                  <a className="detail-link" href={selectedRoute.gpx_download_url} download>
                    {copy.gpxDownload}
                  </a>
                ) : (
                  <button
                    type="button"
                    className="detail-link"
                    disabled
                    title={copy.gpxUnavailable}
                  >
                    {copy.gpxDownload}
                  </button>
                )}
              </div>
              {!selectedRoute.gpx_download_url && (
                <p className="gpx-notice">{copy.gpxUnavailable}</p>
              )}
              <div className="source-section">
                <h3>{copy.sources}</h3>
                {selectedRoute.sources.length ? (
                  <ul className="source-list">
                    {selectedRoute.sources.map((source) => (
                      <li key={source.mapy_url}>
                        <a
                          href={source.mapy_url}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={copy.mapyLink}
                        >
                          {source.title || copy.mapyLink}
                        </a>
                        <span>{routeStatus(source.status, copy)}</span>
                        {source.last_successful_check_at ? (
                          <time dateTime={source.last_successful_check_at}>
                            {copy.sourceLastSuccessfulCheck(
                              new Intl.DateTimeFormat(language === 'cs' ? 'cs-CZ' : 'en-GB', {
                                dateStyle: 'medium',
                              }).format(new Date(source.last_successful_check_at)),
                            )}
                          </time>
                        ) : null}
                        {source.last_checked_at &&
                        source.last_checked_at !== source.last_successful_check_at ? (
                          <time dateTime={source.last_checked_at}>
                            {copy.sourceChecked(
                              new Intl.DateTimeFormat(language === 'cs' ? 'cs-CZ' : 'en-GB', {
                                dateStyle: 'medium',
                              }).format(new Date(source.last_checked_at)),
                            )}
                          </time>
                        ) : null}
                        {source.posts.map((post) => (
                          <span key={post.url} className="source-post">
                            <a
                              href={post.url}
                              target="_blank"
                              rel="noreferrer"
                              aria-label={copy.sourceLink(post.thread_title)}
                            >
                              {post.thread_title || post.url}
                            </a>
                            {post.author && <span> · {post.author}</span>}
                            {post.posted_at && (
                              <time dateTime={post.posted_at}>
                                {' · '}
                                {copy.posted(
                                  new Intl.DateTimeFormat(language === 'cs' ? 'cs-CZ' : 'en-GB', {
                                    dateStyle: 'medium',
                                  }).format(new Date(post.posted_at)),
                                )}
                              </time>
                            )}
                          </span>
                        ))}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="no-sources">{copy.noSources}</p>
                )}
              </div>
            </>
          )}
          <button
            type="button"
            className="close-detail"
            onClick={() => selectRoute(null)}
            aria-label={copy.closeDetails}
          >
            ×
          </button>
        </section>
      )}
      {reportOpen && selectedRoute && (
        <div className="report-backdrop" role="presentation">
          <section
            ref={reportDialogNode}
            className="report-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="report-title"
          >
            <h2 id="report-title">{copy.reportTitle}</h2>
            {reportSent ? (
              <p role="status">{copy.reportThanks}</p>
            ) : (
              <form
                onSubmit={(event) => {
                  event.preventDefault()
                  setReportSent(true)
                }}
              >
                <label htmlFor="report-message">{copy.reportLabel}</label>
                <textarea
                  id="report-message"
                  ref={reportMessageNode}
                  required
                  placeholder={copy.reportPlaceholder}
                  rows={5}
                />
                <div className="report-actions">
                  <button type="submit">{copy.sendReport}</button>
                  <button type="button" onClick={() => setReportOpen(false)}>
                    {copy.cancel}
                  </button>
                </div>
              </form>
            )}
            {reportSent && (
              <button type="button" onClick={() => setReportOpen(false)}>
                {copy.cancel}
              </button>
            )}
          </section>
        </div>
      )}
    </main>
  )
}

export default App
