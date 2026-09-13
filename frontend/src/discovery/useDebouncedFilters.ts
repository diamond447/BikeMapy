import { useEffect, useRef, useState } from 'react'

import type { Filters } from './types'

export const CATALOGUE_FILTER_DEBOUNCE_MS = 250

export type DebouncedFilters = { filters: Filters; revision: number }

/** Keep the controls and URL responsive while waiting for a settled filter value. */
export function useDebouncedFilters(filters: Filters): DebouncedFilters {
  const [debouncedFilters, setDebouncedFilters] = useState<DebouncedFilters>({
    filters,
    revision: 0,
  })
  const previousFilters = useRef(filters)

  useEffect(() => {
    if (previousFilters.current === filters) return
    previousFilters.current = filters
    const timeout = window.setTimeout(() => {
      setDebouncedFilters((current) => ({
        filters,
        revision: current.revision + 1,
      }))
    }, CATALOGUE_FILTER_DEBOUNCE_MS)
    return () => window.clearTimeout(timeout)
  }, [filters])

  return debouncedFilters
}
