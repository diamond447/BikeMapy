import type { RefObject } from 'react'

import type { Filters } from '../discovery/types'
import type { Copy } from '../i18n/types'

export type MapStageProps = {
  mapNode: RefObject<HTMLDivElement | null>
  copy: Copy
  filters: Filters
  hoveredRouteId: string | null
  loading: boolean
  error: string | null
  mapError: string | null
  mapMode: 'heatmap' | 'routes' | undefined
  routeCount: number
  onRetryMap: () => void
  onRetryData: () => void
}

export function MapStage({
  mapNode,
  copy,
  filters,
  hoveredRouteId,
  loading,
  error,
  mapError,
  mapMode,
  routeCount,
  onRetryMap,
  onRetryData,
}: MapStageProps) {
  return (
    <section className="map-stage" aria-label={copy.map}>
      <div
        ref={mapNode}
        className="map-canvas"
        role="application"
        aria-label={copy.interactiveMap}
        data-hovered-route={hoveredRouteId ?? undefined}
      />
      <div className="map-vignette" aria-hidden="true" />
      <div className="map-status" role="status">
        {loading
          ? copy.mapLoading
          : error
            ? error
            : Object.values(filters).some(Boolean) && mapMode === 'heatmap'
              ? copy.filteredDensity
              : mapMode === 'heatmap'
                ? copy.density
                : copy.routesInView(routeCount)}
      </div>
      <div className="map-empty">
        {(mapError || error) && (
          <>
            <strong>{mapError ?? copy.mapPaused}</strong>
            <button type="button" onClick={mapError ? onRetryMap : onRetryData}>
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
  )
}
