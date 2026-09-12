import type { RefObject } from 'react'

import { trackProductEvent } from '../analytics'
import type { Route } from '../discovery/types'
import type { Copy, Language } from '../i18n/types'
import {
  ElevationProfile,
  RouteSources,
  formatMetric,
  hasMetric,
  routeStatus,
} from './RouteDetailParts'
import { SidebarFooter } from './SidebarFooter'

type RouteDetailData = {
  selectedRoute: Route | null | undefined
  selectedMetadataError: string | null
  selectedGeometryError: string | null
  selectedMetadataLoading: boolean
  selectedGeometryLoading: boolean
  overlap: string[]
  selectedIndex: number
  permanentRouteUrl: string
  shareState: 'idle' | 'copied' | 'failed'
  reportsEnabled: boolean
  categoriesEnabled: boolean
}

type RouteDetailActions = {
  onBack: () => void
  onNextOverlap: (direction: number) => void
  onRetryMetadata: () => void
  onRetryGeometry: () => void
  onCopyLink: () => void
  onOpenReport: () => void
  onClose: () => void
  onToggleLanguage: () => void
}

export type RouteDetailProps = {
  detailNode: RefObject<HTMLElement | null>
  reportTriggerNode: RefObject<HTMLButtonElement | null>
  copy: Copy
  language: Language
  data: RouteDetailData
  actions: RouteDetailActions
}

export function RouteDetail({
  detailNode,
  reportTriggerNode,
  copy,
  language,
  data,
  actions,
}: RouteDetailProps) {
  const {
    categoriesEnabled,
    selectedRoute,
    selectedMetadataError,
    selectedGeometryError,
    selectedMetadataLoading,
    selectedGeometryLoading,
    overlap,
    selectedIndex,
    permanentRouteUrl,
    shareState,
    reportsEnabled,
  } = data
  const {
    onBack,
    onNextOverlap,
    onRetryMetadata,
    onRetryGeometry,
    onCopyLink,
    onOpenReport,
    onClose,
    onToggleLanguage,
  } = actions
  return (
    <section ref={detailNode} className="route-detail" aria-labelledby="route-detail-title">
      <button type="button" className="back-results" onClick={onBack}>
        ← {copy.backToResults}
      </button>
      <div className="detail-kicker">
        {copy.selectedRoute}{' '}
        {overlap.length > 1 && (
          <span className="overlap-picker">
            <button
              type="button"
              onClick={() => onNextOverlap(-1)}
              aria-label={copy.previousOverlap}
            >
              ‹
            </button>
            <span>
              {Math.max(1, selectedIndex + 1)} / {overlap.length}
            </span>
            <button type="button" onClick={() => onNextOverlap(1)} aria-label={copy.nextOverlap}>
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
                  <dd>{selectedRoute.loop_status === 'loop' ? copy.loop : copy.pointToPoint}</dd>
                </div>
              )}
            </dl>
          )}
          {selectedRoute.elevation_profile && (
            <ElevationProfile points={selectedRoute.elevation_profile} copy={copy} />
          )}
          {categoriesEnabled && selectedRoute.categories.length > 0 && (
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
        <button type="button" className="detail-retry" onClick={onRetryMetadata}>
          {copy.retryDetails}
        </button>
      )}
      {selectedGeometryError && (
        <button type="button" className="detail-retry" onClick={onRetryGeometry}>
          {copy.retryGeometry}
        </button>
      )}
      {selectedRoute && (
        <>
          <RouteSources route={selectedRoute} copy={copy} language={language} />
          <div className="detail-actions" aria-label={copy.share}>
            <a className="detail-link" href={permanentRouteUrl}>
              {copy.share}
            </a>
            <button type="button" className="detail-link" onClick={onCopyLink}>
              {shareState === 'copied'
                ? copy.copied
                : shareState === 'failed'
                  ? copy.copyFailed
                  : copy.copyLink}
            </button>
            {selectedRoute.gpx_download_url ? (
              <a
                className="detail-link"
                href={selectedRoute.gpx_download_url}
                download
                onClick={() => trackProductEvent('gpx_download_click')}
              >
                {copy.gpxDownload}
              </a>
            ) : (
              <button type="button" className="detail-link" disabled title={copy.gpxUnavailable}>
                {copy.gpxDownload}
              </button>
            )}
          </div>
          {!selectedRoute.gpx_download_url && <p className="gpx-notice">{copy.gpxUnavailable}</p>}
          {reportsEnabled && (
            <button
              type="button"
              className="detail-link detail-report"
              ref={reportTriggerNode}
              onClick={onOpenReport}
            >
              {copy.report}
            </button>
          )}
        </>
      )}
      <SidebarFooter copy={copy} language={language} onToggleLanguage={onToggleLanguage} />
      <button
        type="button"
        className="close-detail"
        onClick={onClose}
        aria-label={copy.closeDetails}
      >
        ×
      </button>
    </section>
  )
}
