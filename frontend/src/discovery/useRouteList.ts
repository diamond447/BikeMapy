import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { apiClient } from '../api/client'
import type { Filters, Route, ViewState } from './types'
import { useDebouncedFilters } from './useDebouncedFilters'

export type RouteListState = {
  routes: Route[]
  count: number
  next: string | null
  loading: boolean
  loadingMore: boolean
  error: string | null
  loadMoreError: string | null
  loadMoreRetryable: boolean
}

export type RouteNextRequest = { key: string; query: Record<string, string> }
export type RouteListCopy = { unavailable: string; incompleteResults: string }

export function nextRouteRequest(next: string): RouteNextRequest {
  const url = new URL(next, window.location.origin)
  const query = Object.fromEntries(url.searchParams.entries())
  const paginationKey = ['page', 'offset', 'cursor'].find((key) => Boolean(query[key]?.trim()))
  if (!paginationKey) throw new Error('Pagination link has no cursor.')
  url.searchParams.sort()
  return { key: `${url.pathname}?${url.searchParams.toString()}`, query }
}

export function uniqueRoutes(routes: Route[]): Route[] {
  const known = new Set<string>()
  return routes.filter((route) => {
    if (known.has(route.id)) return false
    known.add(route.id)
    return true
  })
}

const emptyState = (): RouteListState => ({
  routes: [],
  count: 0,
  next: null,
  loading: true,
  loadingMore: false,
  error: null,
  loadMoreError: null,
  loadMoreRetryable: false,
})

export function useRouteList(
  filters: Filters,
  view: ViewState,
  viewportOnly: boolean,
  retryToken: number,
  copy: RouteListCopy,
) {
  const [state, setState] = useState<RouteListState>(emptyState)
  const requestIdRef = useRef(0)
  const requestControllerRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  const debouncedFilters = useDebouncedFilters(filters)
  const paginationRef = useRef<{
    requestId: number
    requested: Set<string>
    visited: Set<string>
    inFlight: boolean
  }>({ requestId: 0, requested: new Set(), visited: new Set(), inFlight: false })
  const query = useMemo(() => {
    const params = new URLSearchParams({ page_size: '100' })
    Object.entries(debouncedFilters.filters).forEach(([key, value]) => {
      if (value) params.set(key, value)
    })
    if (viewportOnly && view.bounds) {
      params.set('west', String(view.bounds[0]))
      params.set('south', String(view.bounds[1]))
      params.set('east', String(view.bounds[2]))
      params.set('north', String(view.bounds[3]))
    }
    return params.toString()
  }, [debouncedFilters.filters, view.bounds, viewportOnly])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      requestControllerRef.current?.abort()
      requestControllerRef.current = null
      requestIdRef.current += 1
    }
  }, [])

  const previousFiltersRef = useRef(filters)
  useEffect(() => {
    if (previousFiltersRef.current === filters) return
    previousFiltersRef.current = filters
    requestIdRef.current += 1
    requestControllerRef.current?.abort()
    requestControllerRef.current = null
  }, [filters])

  useEffect(() => {
    const requestId = ++requestIdRef.current
    const controller = new AbortController()
    requestControllerRef.current?.abort()
    requestControllerRef.current = controller
    paginationRef.current = {
      requestId,
      requested: new Set(),
      visited: new Set(),
      inFlight: false,
    }
    let active = true
    // Reset the request state before subscribing to the new API response.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState(emptyState())
    apiClient
      .GET('/api/v1/routes/', {
        params: { query: Object.fromEntries(new URLSearchParams(query)) } as never,
        signal: controller.signal,
      })
      .then(({ data, error }) => {
        if (!mountedRef.current || !active || requestIdRef.current !== requestId) return
        if (error || !data) throw new Error(copy.unavailable)
        const routes = uniqueRoutes(data.results)
        const count = Math.max(data.count, routes.length)
        let next = data.next ?? null
        let loadMoreError: string | null = null
        if (data.count < routes.length) {
          next = null
          loadMoreError = copy.incompleteResults
        } else if (next) {
          try {
            nextRouteRequest(next)
          } catch {
            next = null
            loadMoreError = copy.incompleteResults
          }
        } else if (routes.length < count) loadMoreError = copy.incompleteResults
        setState({
          routes,
          count,
          next,
          loading: false,
          loadingMore: false,
          error: null,
          loadMoreError,
          loadMoreRetryable: false,
        })
      })
      .catch((error: unknown) => {
        if (mountedRef.current && active && requestIdRef.current === requestId)
          setState({
            ...emptyState(),
            loading: false,
            error: error instanceof Error ? error.message : copy.unavailable,
          })
      })
    return () => {
      active = false
      controller.abort()
      if (requestControllerRef.current === controller) requestControllerRef.current = null
    }
  }, [copy.incompleteResults, copy.unavailable, debouncedFilters.revision, query, retryToken])

  const loadMore = useCallback(() => {
    const pagination = paginationRef.current
    if (
      !state.next ||
      state.loadingMore ||
      pagination.inFlight ||
      pagination.requestId !== requestIdRef.current
    )
      return
    const requestId = requestIdRef.current
    let nextRequest: RouteNextRequest
    try {
      nextRequest = nextRouteRequest(state.next)
    } catch {
      setState((current) => ({
        ...current,
        next: null,
        loadingMore: false,
        loadMoreError: copy.incompleteResults,
        loadMoreRetryable: false,
      }))
      return
    }
    if (pagination.requested.has(nextRequest.key) || pagination.visited.has(nextRequest.key)) {
      setState((current) => ({
        ...current,
        next: null,
        loadingMore: false,
        loadMoreError: copy.incompleteResults,
        loadMoreRetryable: false,
      }))
      return
    }
    pagination.requested.add(nextRequest.key)
    pagination.inFlight = true
    const controller = new AbortController()
    requestControllerRef.current?.abort()
    requestControllerRef.current = controller
    setState((current) => ({
      ...current,
      loadingMore: true,
      loadMoreError: null,
      loadMoreRetryable: false,
    }))
    apiClient
      .GET('/api/v1/routes/', {
        params: { query: nextRequest.query } as never,
        signal: controller.signal,
      })
      .then(({ data, error }) => {
        if (!mountedRef.current || requestIdRef.current !== requestId) return
        if (error || !data) throw new Error(copy.unavailable)
        const incoming = uniqueRoutes(data.results)
        const current = state
        const known = new Set(current.routes.map((route) => route.id))
        const appended = incoming.filter((route) => {
          if (known.has(route.id)) return false
          known.add(route.id)
          return true
        })
        let next = data.next ?? null
        let responseNextRequest: RouteNextRequest | null = null
        let structuralError = appended.length === 0 && Boolean(next)
        if (next) {
          try {
            responseNextRequest = nextRouteRequest(next)
            structuralError ||= pagination.requested.has(responseNextRequest.key)
            structuralError ||= pagination.visited.has(responseNextRequest.key)
          } catch {
            next = null
            structuralError = true
          }
        }
        const loaded = current.routes.length + appended.length
        const count = Math.max(data.count, loaded)
        if (data.count < loaded) structuralError = true
        if (!next && loaded < data.count) structuralError = true
        pagination.inFlight = false
        pagination.visited.add(nextRequest.key)
        setState({
          ...current,
          routes: [...current.routes, ...appended],
          count,
          next: structuralError ? null : next,
          loadingMore: false,
          loadMoreError: structuralError ? copy.incompleteResults : null,
          loadMoreRetryable: false,
        })
      })
      .catch((error: unknown) => {
        if (!mountedRef.current || requestIdRef.current !== requestId) return
        pagination.inFlight = false
        pagination.requested.delete(nextRequest.key)
        setState((current) => ({
          ...current,
          loadingMore: false,
          loadMoreError: error instanceof Error ? error.message : copy.unavailable,
          loadMoreRetryable: true,
        }))
      })
      .finally(() => {
        if (requestControllerRef.current === controller) requestControllerRef.current = null
      })
  }, [copy.incompleteResults, copy.unavailable, state])

  return { ...state, loadMore }
}
