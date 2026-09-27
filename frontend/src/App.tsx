/* eslint-disable react-refresh/only-export-components -- state helpers are exported for interaction tests. */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import 'maplibre-gl/dist/maplibre-gl.css'

import { trackProductEvent } from './analytics'
import { normalizePublicSiteUrl, routeUrl } from './siteMetadata'
import { reportsEnabled } from './buildConfig'
import { useRouteList } from './discovery/useRouteList'
import { useViewport } from './discovery/useViewport'
import { useSelectedRoute } from './discovery/useSelectedRoute'
import { useReport } from './discovery/useReport'
import { useMapStage } from './discovery/useMapStage'
import { RouteBrowser } from './components/RouteBrowser'
import { RouteDetail } from './components/RouteDetail'
import { ReportDialog } from './components/ReportDialog'
import { MapStage } from './components/MapStage'
import { GameAccount } from './components/GameAccount'
import type { InteractionMode } from './components/interaction'
import { translations } from './i18n/translations'
import type { Language } from './i18n/types'
import {
  DEFAULT_FILTERS,
  STORAGE_KEY,
  categoriesEnabled,
  normalizeFilters,
  parseState,
  writeUrl,
} from './discovery/state'
import type { Filters, ViewState } from './discovery/types'

export { geometryBounds, geometryCoordinates } from './discovery/geometry'
export { categoriesEnabled, normalizeFilters, parseState, writeUrl } from './discovery/state'

const PUBLIC_SITE_URL = normalizePublicSiteUrl(import.meta.env.VITE_PUBLIC_SITE_URL)
const LANGUAGE_KEY = 'bikemapy:language'
// Cloudflare preview builds set this to false. Keeping the guard at build time
// means a preview contains only the public read API and has no report action.
const REPORTS_ENABLED = reportsEnabled(import.meta.env.VITE_ENABLE_REPORTS)

function initialLanguage(): Language {
  try {
    const saved = localStorage.getItem(LANGUAGE_KEY)
    if (saved === 'en' || saved === 'cs') return saved
  } catch {
    /* private browsing */
  }
  return navigator.language.toLowerCase().startsWith('cs') ? 'cs' : 'en'
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

function App() {
  const initial = useMemo(parseState, [])
  const [language, setLanguage] = useState<Language>(initialLanguage)
  const copy = translations[language]
  const categoriesEnabledForBuild = categoriesEnabled()
  const [filters, setFilters] = useState<Filters>(() => normalizeFilters(initial.filters))
  const [view, setView] = useState<ViewState>(initial.view)
  const [selectedId, setSelectedId] = useState<string | null>(initial.routeId)
  const [metadataRetryToken, setMetadataRetryToken] = useState(0)
  const [geometryRetryToken, setGeometryRetryToken] = useState(0)
  const [overlap, setOverlap] = useState<string[]>([])
  const isMobileViewport = typeof window !== 'undefined' && window.innerWidth <= 700
  const [panelOpen, setPanelOpen] = useState(!isMobileViewport)
  const [mobileSheetPosition, setMobileSheetPosition] = useState<'collapsed' | 'half' | 'full'>(
    isMobileViewport ? 'collapsed' : 'full',
  )
  const [hoveredRouteId, setHoveredRouteId] = useState<string | null>(null)
  const [mapReady, setMapReady] = useState(false)
  const [viewportOnly, setViewportOnly] = useState(initial.viewportOnly)
  const [retryToken, setRetryToken] = useState(0)
  const [mapRetry, setMapRetry] = useState(0)
  const [shareState, setShareState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [mobilePanelHeight, setMobilePanelHeight] = useState<number | null>(null)
  const [mobileDetailHeight, setMobileDetailHeight] = useState<number | null>(null)
  const trackedRouteRef = useRef<string | null>(null)
  const mapNode = useRef<HTMLDivElement>(null)
  const panelNode = useRef<HTMLElement>(null)
  const detailNode = useRef<HTMLElement>(null)
  const detailHeadingNode = useRef<HTMLHeadingElement>(null)
  const originatingRouteRef = useRef<string | null>(initial.routeId)
  const keyboardSelectionRef = useRef(false)
  const pendingDetailFocusRef = useRef(false)
  const pendingRouteListFocusRef = useRef<string | null>(null)
  const {
    routes,
    count,
    next,
    loading,
    loadingMore,
    loadMore,
    loadMoreError,
    loadMoreRetryable,
    error,
  } = useRouteList(filters, view, viewportOnly, retryToken, copy)
  const viewport = useViewport(view, filters, mapReady, retryToken, copy)
  const selectedState = useSelectedRoute(
    selectedId,
    retryToken,
    metadataRetryToken,
    geometryRetryToken,
    copy,
  )
  const {
    geometry: selected,
    record: selectedRecord,
    metadataError: selectedMetadataError,
    geometryError: selectedGeometryError,
    metadataLoading: selectedMetadataLoading,
    geometryLoading: selectedGeometryLoading,
  } = selectedState
  const selectedRoute = routes.find((route) => route.id === selectedId) ?? selectedRecord
  const report = useReport(selectedRoute, copy, REPORTS_ENABLED)
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
  const permanentRouteUrl = selectedRoute
    ? routeUrl(PUBLIC_SITE_URL, selectedRoute.id, selectedRoute.slug)
    : ''
  const panelToggleLabel =
    window.innerWidth <= 700
      ? mobileSheetPosition === 'collapsed'
        ? copy.showRoutes
        : mobileSheetPosition === 'half'
          ? copy.sheetHalf
          : copy.hideRoutes
      : panelOpen
        ? copy.hideRoutes
        : copy.showRoutes

  useEffect(() => {
    if (!selectedId || trackedRouteRef.current === selectedId) return
    trackedRouteRef.current = selectedId
    trackProductEvent('route_detail_view')
  }, [selectedId])

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
    (id: string | null, push = true, interaction: InteractionMode = 'programmatic') => {
      if (!id) {
        trackedRouteRef.current = null
        pendingDetailFocusRef.current = false
        pendingRouteListFocusRef.current =
          interaction === 'keyboard' ? originatingRouteRef.current : null
        originatingRouteRef.current = null
        keyboardSelectionRef.current = false
      } else {
        pendingDetailFocusRef.current = interaction === 'keyboard'
        originatingRouteRef.current = id
        keyboardSelectionRef.current = interaction === 'keyboard'
        pendingRouteListFocusRef.current = null
      }
      setSelectedId(id)
      setOverlap(id ? [id] : [])
      if (window.innerWidth <= 700) {
        setMobilePanelHeight(null)
        if (id) {
          setPanelOpen(false)
          setMobileSheetPosition('full')
        } else if (interaction === 'keyboard') {
          setPanelOpen(true)
          setMobileSheetPosition('half')
        } else {
          setMobileSheetPosition('collapsed')
        }
      }
      if (push) writeUrl(filters, view, id, 'push', viewportOnly)
    },
    [filters, view, viewportOnly],
  )

  useEffect(() => {
    if (!selectionVisible || !pendingDetailFocusRef.current) return
    const heading = detailHeadingNode.current
    if (!heading) return
    pendingDetailFocusRef.current = false
    heading.focus()
  }, [selectedId, selectedRoute, selectionVisible])

  useEffect(() => {
    if (selectionVisible || !pendingRouteListFocusRef.current) return
    const routeId = pendingRouteListFocusRef.current
    pendingRouteListFocusRef.current = null
    const card = Array.from(document.querySelectorAll<HTMLButtonElement>('.route-card')).find(
      (candidate) => candidate.dataset.routeId === routeId,
    )
    ;(card ?? document.querySelector<HTMLElement>('.route-list'))?.focus()
  }, [selectionVisible])
  const filtersRef = useRef(filters)
  const viewRef = useRef(view)
  const viewportOnlyRef = useRef(viewportOnly)
  filtersRef.current = filters
  viewRef.current = view
  viewportOnlyRef.current = viewportOnly
  const mapStage = useMapStage({
    mapNode,
    view,
    mapData: viewport.data,
    selected,
    selectedId,
    hoveredRouteId,
    panelOpen,
    mobilePanelHeight,
    mobileDetailHeight,
    mapRetry,
    copy,
    onViewChange: setView,
    onRouteClick: (ids) => {
      pendingDetailFocusRef.current = false
      originatingRouteRef.current = ids[0]
      keyboardSelectionRef.current = false
      pendingRouteListFocusRef.current = null
      setOverlap(ids)
      setSelectedId(ids[0])
      writeUrl(filtersRef.current, viewRef.current, ids[0], 'push', viewportOnlyRef.current)
    },
    onHover: setHoveredRouteId,
    onReady: setMapReady,
  })
  const { jumpTo } = mapStage

  useEffect(() => {
    if (selectedId && window.innerWidth <= 700) {
      setMobilePanelHeight(null)
      setPanelOpen(false)
    }
  }, [selectedId])
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
      const returningToResults = !next.routeId && Boolean(selectedId)
      const focusInDetail = Boolean(
        detailNode.current && detailNode.current.contains(document.activeElement),
      )
      const restoreResultsFocus =
        returningToResults && (keyboardSelectionRef.current || focusInDetail)
      setFilters(normalizeFilters(next.filters))
      setView(next.view)
      setSelectedId(next.routeId)
      setOverlap(next.routeId ? [next.routeId] : [])
      setViewportOnly(next.viewportOnly)
      pendingDetailFocusRef.current = false
      pendingRouteListFocusRef.current = restoreResultsFocus
        ? (originatingRouteRef.current ?? selectedId)
        : null
      originatingRouteRef.current = next.routeId
      keyboardSelectionRef.current = false
      if (restoreResultsFocus && window.innerWidth <= 700) {
        setPanelOpen(true)
        setMobileSheetPosition('half')
        setMobilePanelHeight(null)
      }
      jumpTo(next.view)
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [jumpTo, selectedId])
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
  const setSheetPosition = (position: 'collapsed' | 'half' | 'full') => {
    if (position === 'collapsed') {
      setPanelOpen(false)
      setMobileSheetPosition('collapsed')
    } else {
      setPanelOpen(true)
      setMobileSheetPosition(position)
    }
    setMobilePanelHeight(null)
  }
  const nextOverlap = (direction: number) => {
    if (overlap.length < 2) return
    const nextIndex = (Math.max(0, selectedIndex) + direction + overlap.length) % overlap.length
    setSelectedId(overlap[nextIndex])
    writeUrl(filters, view, overlap[nextIndex], 'push', viewportOnly)
  }
  const handlePanelToggle = () => {
    if (mobileSelectionActive) {
      selectRoute(null, false)
      return
    }
    if (window.innerWidth <= 700) {
      const next =
        mobileSheetPosition === 'collapsed'
          ? 'half'
          : mobileSheetPosition === 'half'
            ? 'full'
            : 'collapsed'
      setSheetPosition(next)
    } else setSheetPosition(panelOpen ? 'collapsed' : 'full')
  }
  return (
    <main
      className="app-shell"
      data-sheet-position={mobileSheetPosition}
      style={
        {
          '--mobile-sheet-height':
            mobileSheetPosition === 'collapsed'
              ? '42px'
              : mobileSheetPosition === 'half'
                ? '50vh'
                : '82vh',
        } as CSSProperties
      }
    >
      <span className="sr-only" data-testid="sheet-position" data-position={mobileSheetPosition}>
        {mobileSheetPosition}
      </span>
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
        <GameAccount copy={copy} initialOpen={window.location.pathname === '/game'} />
      </header>
      <MapStage
        mapNode={mapNode}
        copy={copy}
        filters={filters}
        hoveredRouteId={hoveredRouteId}
        loading={viewport.loading}
        error={viewport.error}
        mapError={mapStage.mapError}
        mapMode={viewport.data?.mode}
        routeCount={viewport.data?.routes.length ?? 0}
        onRetryMap={() => setMapRetry((current) => current + 1)}
        onRetryData={retry}
      />
      <div className="route-sheet">
        <RouteBrowser
          panelNode={panelNode}
          copy={copy}
          language={language}
          data={{
            filters,
            loading,
            error,
            routes: displayRoutes,
            count,
            next,
            loadingMore,
            loadMoreError,
            loadMoreRetryable,
            selectedId,
            hoveredRouteId,
            viewportOnly,
            viewportMode: viewport.data?.mode,
          }}
          ui={{
            categoriesEnabled: categoriesEnabledForBuild,
            panelOpen,
            mobileSelectionActive,
            panelToggleLabel,
            showFooter: !selectionVisible,
          }}
          actions={{
            onPanelToggle: handlePanelToggle,
            onUpdateFilter: updateFilter,
            onResetFilters: resetFilters,
            onViewportOnlyChange: setViewportOnly,
            onRetry: retry,
            onHover: setHoveredRouteId,
            onSelectRoute: (id, interaction) => selectRoute(id, true, interaction),
            onLoadMore: loadMore,
            onToggleLanguage: toggleLanguage,
          }}
        />
        {selectionVisible && (
          <RouteDetail
            detailNode={detailNode}
            headingNode={detailHeadingNode}
            reportTriggerNode={report.triggerNode}
            copy={copy}
            language={language}
            data={{
              categoriesEnabled: categoriesEnabledForBuild,
              selectedRoute,
              selectedMetadataError,
              selectedGeometryError,
              selectedMetadataLoading,
              selectedGeometryLoading,
              overlap,
              selectedIndex,
              permanentRouteUrl,
              shareState,
              reportsEnabled: REPORTS_ENABLED,
            }}
            actions={{
              onBack: (interaction) => selectRoute(null, true, interaction),
              onNextOverlap: nextOverlap,
              onRetryMetadata: () => setMetadataRetryToken((current) => current + 1),
              onRetryGeometry: () => setGeometryRetryToken((current) => current + 1),
              onCopyLink: copyPermanentLink,
              onOpenReport: report.openForm,
              onClose: (interaction) => selectRoute(null, true, interaction),
              onToggleLanguage: toggleLanguage,
            }}
          />
        )}
      </div>
      {report.open && selectedRoute && (
        <ReportDialog
          copy={copy}
          route={selectedRoute}
          controller={report}
          onCancel={() => report.setOpen(false)}
        />
      )}
    </main>
  )
}

export default App
