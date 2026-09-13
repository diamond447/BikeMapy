import { useEffect, useRef, useState } from 'react'

import { apiClient } from '../api/client'
import type { Filters, ViewState, ViewportResponse } from './types'
import { useDebouncedFilters } from './useDebouncedFilters'

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
  const requestIdRef = useRef(0)
  const requestControllerRef = useRef<AbortController | null>(null)
  const debouncedFilters = useDebouncedFilters(filters)
  const previousFiltersRef = useRef(filters)
  useEffect(() => {
    if (previousFiltersRef.current === filters) return
    previousFiltersRef.current = filters
    requestIdRef.current += 1
    requestControllerRef.current?.abort()
    requestControllerRef.current = null
  }, [filters])
  useEffect(() => {
    if (!ready) return
    const requestId = ++requestIdRef.current
    const controller = new AbortController()
    requestControllerRef.current?.abort()
    requestControllerRef.current = controller
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
      ...Object.fromEntries(Object.entries(debouncedFilters).filter(([, value]) => value)),
    }
    // Mark the external request as pending while it is in flight.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState((current) => ({ ...current, loading: true, error: null }))
    apiClient
      .GET('/api/v1/routes/viewport/', {
        params: { query } as never,
        signal: controller.signal,
      })
      .then(({ data, error }) => {
        if (!active || requestIdRef.current !== requestId) return
        if (error || !data) throw new Error(copy.mapUnavailable)
        setState({ data, loading: false, error: null })
      })
      .catch((error: unknown) => {
        if (active && requestIdRef.current === requestId)
          setState((current) => ({
            ...current,
            loading: false,
            error: error instanceof Error ? error.message : copy.mapUnavailable,
          }))
      })
      .finally(() => {
        if (requestControllerRef.current === controller) requestControllerRef.current = null
      })
    return () => {
      active = false
      controller.abort()
      if (requestControllerRef.current === controller) requestControllerRef.current = null
    }
  }, [
    copy.mapUnavailable,
    debouncedFilters,
    ready,
    retryToken,
    view.bounds,
    view.latitude,
    view.longitude,
    view.zoom,
  ])
  return state
}
