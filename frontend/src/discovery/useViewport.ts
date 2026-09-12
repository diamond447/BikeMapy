import { useEffect, useState } from 'react'

import { apiClient } from '../api/client'
import type { Filters, ViewState, ViewportResponse } from './types'

export type ViewportCopy = { mapUnavailable: string }

export function useViewport(
  view: ViewState,
  filters: Filters,
  ready: boolean,
  retryToken: number,
  copy: ViewportCopy,
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
    // Mark the external request as pending while it is in flight.
    // eslint-disable-next-line react-hooks/set-state-in-effect
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
