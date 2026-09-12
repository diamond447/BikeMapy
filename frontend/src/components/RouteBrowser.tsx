import type { RefObject } from 'react'

import { hasMetric } from './RouteDetailParts'
import { SidebarFooter } from './SidebarFooter'
import type { Filters, Route } from '../discovery/types'
import type { Copy, Language } from '../i18n/types'

type ViewportMode = 'heatmap' | 'routes' | undefined

type RouteBrowserData = {
  filters: Filters
  routes: Route[]
  count: number
  next: string | null
  loading: boolean
  error: string | null
  loadingMore: boolean
  loadMoreError: string | null
  loadMoreRetryable: boolean
  selectedId: string | null
  hoveredRouteId: string | null
  viewportOnly: boolean
  viewportMode: ViewportMode
}

type RouteBrowserUi = {
  categoriesEnabled: boolean
  panelOpen: boolean
  mobileSelectionActive: boolean
  panelToggleLabel: string
  showFooter: boolean
}

type RouteBrowserActions = {
  onPanelToggle: () => void
  onUpdateFilter: (key: keyof Filters, value: string) => void
  onResetFilters: () => void
  onViewportOnlyChange: (value: boolean) => void
  onRetry: () => void
  onHover: (id: string | null) => void
  onSelectRoute: (id: string) => void
  onLoadMore: () => void
  onToggleLanguage: () => void
}

export type RouteBrowserProps = {
  panelNode: RefObject<HTMLElement | null>
  copy: Copy
  language: Language
  data: RouteBrowserData
  ui: RouteBrowserUi
  actions: RouteBrowserActions
}

export function RouteBrowser({ panelNode, copy, language, data, ui, actions }: RouteBrowserProps) {
  const {
    filters,
    routes,
    count,
    next,
    loading,
    error,
    loadingMore,
    loadMoreError,
    loadMoreRetryable,
    selectedId,
    hoveredRouteId,
    viewportOnly,
    viewportMode,
  } = data
  const { categoriesEnabled, panelOpen, mobileSelectionActive, panelToggleLabel, showFooter } = ui
  const {
    onPanelToggle,
    onUpdateFilter,
    onResetFilters,
    onViewportOnlyChange,
    onRetry,
    onHover,
    onSelectRoute,
    onLoadMore,
    onToggleLanguage,
  } = actions
  return (
    <aside
      ref={panelNode}
      className={`route-panel ${panelOpen ? 'is-open' : 'is-collapsed'}`}
      aria-label={copy.searchRoutes}
    >
      <button
        type="button"
        className="panel-toggle"
        onClick={onPanelToggle}
        disabled={mobileSelectionActive}
        aria-expanded={panelOpen}
        aria-controls="route-browser"
      >
        {panelToggleLabel} <span aria-hidden="true">{panelOpen ? '−' : '+'}</span>
      </button>
      <div id="route-browser" className="panel-content">
        <p className="eyebrow">{copy.atlasKicker}</p>
        <div className="atlas-heading">
          <h1>{copy.heading}</h1>
          <p className="intro">{copy.intro}</p>
        </div>
        <label className="search-field">
          <span aria-hidden="true">⌕</span>
          <span className="sr-only">{copy.searchRoutes}</span>
          <input
            type="search"
            aria-label={copy.searchRoutes}
            value={filters.search}
            onChange={(event) => onUpdateFilter('search', event.target.value)}
            placeholder={copy.searchPlaceholder}
          />
        </label>
        <div className="filter-grid">
          <label>
            {copy.author}
            <input
              aria-label={copy.author}
              value={filters.author}
              onChange={(event) => onUpdateFilter('author', event.target.value)}
              placeholder={copy.authorPlaceholder}
            />
          </label>
          {categoriesEnabled && (
            <label>
              {copy.category}
              <input
                aria-label={copy.category}
                value={filters.category}
                onChange={(event) => onUpdateFilter('category', event.target.value)}
                placeholder={copy.categoryPlaceholder}
              />
            </label>
          )}
          {(
            [
              ['min_distance_m', copy.distanceFrom],
              ['max_distance_m', copy.distanceTo],
              ['min_ascent_m', copy.climbFrom],
              ['max_ascent_m', copy.climbTo],
            ] as const
          ).map(([key, label]) => (
            <label key={key}>
              {label}
              <input
                aria-label={label}
                inputMode="numeric"
                value={filters[key]}
                onChange={(event) => onUpdateFilter(key, event.target.value)}
              />
            </label>
          ))}
        </div>
        {!categoriesEnabled && (
          <div className="category-toggle" role="group" aria-describedby="category-support-hint">
            <label>
              <input type="checkbox" disabled aria-label={copy.categoriesToggle} />
              <span>{copy.categoriesToggle}</span>
            </label>
            <small id="category-support-hint">{copy.categoriesHint}</small>
          </div>
        )}
        <div className="results-heading">
          <span>{loading ? copy.reading : error ? copy.unavailable : copy.resultHeading}</span>
          <button
            type="button"
            onClick={onResetFilters}
            disabled={!Object.values(filters).some(Boolean)}
          >
            {copy.clearFilters}
          </button>
        </div>
        <label className="viewport-filter">
          <input
            type="checkbox"
            checked={viewportOnly}
            onChange={(event) => onViewportOnlyChange(event.target.checked)}
          />
          <span>{copy.currentViewport}</span>
          {viewportOnly && viewportMode === 'heatmap' && <small>{copy.viewportHint}</small>}
        </label>
        {error && (
          <div className="notice error-notice">
            <strong>{copy.lostConnection}</strong>
            <span>{copy.checkApi}</span>
            <button type="button" onClick={onRetry}>
              {copy.retry}
            </button>
          </div>
        )}
        {!loading && !error && routes.length === 0 && (
          <div className="notice">
            <strong>{copy.noMatches}</strong>
            <span>{copy.widerSearch}</span>
            <button type="button" onClick={onResetFilters}>
              {copy.clearFilters}
            </button>
          </div>
        )}
        <nav className="route-list" aria-label={copy.routes}>
          {routes.map((route) => (
            <button
              key={route.id}
              type="button"
              className={`route-card ${route.id === selectedId ? 'is-selected' : ''} ${route.id === hoveredRouteId ? 'is-hovered' : ''}`}
              aria-current={route.id === selectedId ? 'true' : undefined}
              onMouseEnter={() => onHover(route.id)}
              onMouseLeave={() => onHover(null)}
              onFocus={() => onHover(route.id)}
              onBlur={() => onHover(null)}
              onClick={() => onSelectRoute(route.id)}
            >
              <span className="route-card-marker" aria-hidden="true">
                {route.id === selectedId ? '◆' : '◇'}
              </span>
              <span className="route-card-title">{route.title}</span>
              {(categoriesEnabled && route.categories[0]) || hasMetric(route.distance_m) ? (
                <span className="route-card-meta">
                  {categoriesEnabled && route.categories[0] && (
                    <span>{route.categories[0].name}</span>
                  )}
                  {hasMetric(route.distance_m) && (
                    <span>{(Number(route.distance_m) / 1000).toFixed(1)} km</span>
                  )}
                </span>
              ) : null}
            </button>
          ))}
        </nav>
        {!loading && !error && routes.length > 0 && (
          <div className="route-pagination" aria-live="polite">
            <span className="route-progress" role="status">
              {copy.resultsProgress(routes.length, count)}
            </span>
            {loadMoreError ? (
              <div className="route-pagination-error">
                <span>{loadMoreRetryable ? copy.loadMoreFailed : copy.incompleteResults}</span>
                {loadMoreRetryable && (
                  <button type="button" onClick={onLoadMore} disabled={loadingMore}>
                    {copy.retryLoadMore}
                  </button>
                )}
              </div>
            ) : next ? (
              <button
                type="button"
                onClick={onLoadMore}
                disabled={loadingMore}
                aria-busy={loadingMore}
              >
                {loadingMore ? copy.loadingMore : copy.loadMore}
              </button>
            ) : routes.length >= count ? (
              <span className="route-pagination-complete">{copy.allResultsLoaded}</span>
            ) : (
              <span className="route-pagination-complete">{copy.incompleteResults}</span>
            )}
          </div>
        )}
        {showFooter && (
          <SidebarFooter copy={copy} language={language} onToggleLanguage={onToggleLanguage} />
        )}
      </div>
    </aside>
  )
}
