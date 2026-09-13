import { useEffect, useRef, useState } from 'react'

import type { Filters } from './types'

export const CATALOGUE_FILTER_DEBOUNCE_MS = 250

/** Keep the controls and URL responsive while waiting for a settled filter value. */
export function useDebouncedFilters(filters: Filters): Filters {
  const [debouncedFilters, setDebouncedFilters] = useState(filters)
  const previousFilters = useRef(filters)

  useEffect(() => {
    if (previousFilters.current === filters) return
    previousFilters.current = filters
    const timeout = window.setTimeout(
      () => setDebouncedFilters(filters),
      CATALOGUE_FILTER_DEBOUNCE_MS,
    )
    return () => window.clearTimeout(timeout)
  }, [filters])

  return debouncedFilters
}
