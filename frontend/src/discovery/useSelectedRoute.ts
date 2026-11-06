/* eslint-disable react-hooks/set-state-in-effect -- these effects mirror API request state. */
import { useEffect, useState } from 'react'

import { apiClient } from '../api/client'
import type { RouteRecord, SpatialRoute } from './types'

export type SelectedRouteCopy = {
  detailsUnavailable: string
  geometryUnavailable: string
}

export type SelectedRouteState = {
  geometry: SpatialRoute | null
  record: RouteRecord | null
  metadataError: string | null
  geometryError: string | null
  metadataLoading: boolean
  geometryLoading: boolean
}

export function useSelectedRoute(
  selectedId: string | null,
  retryToken: number,
  metadataRetryToken: number,
  geometryRetryToken: number,
  copy: SelectedRouteCopy,
): SelectedRouteState {
  const [geometry, setGeometry] = useState<SpatialRoute | null>(null)
  // The detail record remembers the route it was requested for, so a record
  // of the previous selection is never returned for the next one.
  const [loaded, setLoaded] = useState<{ id: string; record: RouteRecord } | null>(null)
  const [metadataError, setMetadataError] = useState<string | null>(null)
  const [geometryError, setGeometryError] = useState<string | null>(null)
  const [metadataLoading, setMetadataLoading] = useState(false)
  const [geometryLoading, setGeometryLoading] = useState(false)

  useEffect(() => {
    if (!selectedId) {
      setGeometry(null)
      setLoaded(null)
      setMetadataError(null)
      setGeometryError(null)
      setMetadataLoading(false)
      setGeometryLoading(false)
      return
    }
    setLoaded(null)
    setMetadataError(null)
    setGeometryError(null)
    setMetadataLoading(true)
    setGeometryLoading(true)
  }, [selectedId])

  useEffect(() => {
    if (!selectedId) return
    let active = true
    setMetadataLoading(true)
    setMetadataError(null)
    apiClient
      .GET('/api/v1/routes/{route_id}/', { params: { path: { route_id: selectedId } } })
      .then(({ data, error }) => {
        if (!active) return
        if (error || !data) setMetadataError(copy.detailsUnavailable)
        else {
          setLoaded({ id: selectedId, record: data })
          setMetadataError(null)
        }
        setMetadataLoading(false)
      })
      .catch(() => {
        if (active) {
          setMetadataError(copy.detailsUnavailable)
          setMetadataLoading(false)
        }
      })
    return () => {
      active = false
    }
  }, [copy.detailsUnavailable, metadataRetryToken, retryToken, selectedId])

  useEffect(() => {
    if (!selectedId) return
    let active = true
    setGeometryLoading(true)
    setGeometryError(null)
    apiClient
      .GET('/api/v1/routes/{route_id}/geometry/', { params: { path: { route_id: selectedId } } })
      .then(({ data, error }) => {
        if (active) {
          if (error || !data) {
            setGeometry(null)
            setGeometryError(copy.geometryUnavailable)
          } else {
            setGeometry(data)
            setGeometryError(null)
          }
          setGeometryLoading(false)
        }
      })
      .catch(() => {
        if (active) {
          setGeometry(null)
          setGeometryError(copy.geometryUnavailable)
          setGeometryLoading(false)
        }
      })
    return () => {
      active = false
    }
  }, [copy.geometryUnavailable, geometryRetryToken, retryToken, selectedId])

  return {
    geometry,
    record: loaded && loaded.id === selectedId ? loaded.record : null,
    metadataError,
    geometryError,
    metadataLoading,
    geometryLoading,
  }
}
