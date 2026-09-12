import { DEFAULT_VIEW } from '../mapProvider'
import type { DiscoveryState, Filters, ViewState } from './types'

export const DEFAULT_FILTERS: Filters = {
  search: '',
  author: '',
  category: '',
  min_distance_m: '',
  max_distance_m: '',
  min_ascent_m: '',
  max_ascent_m: '',
}
export const STORAGE_KEY = 'bikemapy:discovery-state'

export function categoriesEnabled(): boolean {
  return import.meta.env.VITE_ENABLE_CATEGORIES === 'true'
}

export function normalizeFilters(filters: Filters): Filters {
  return categoriesEnabled() ? filters : { ...filters, category: '' }
}

export function parseState(): DiscoveryState {
  let saved: Partial<DiscoveryState> = {}
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as typeof saved
  } catch {
    /* malformed preference */
  }
  const params = new URLSearchParams(window.location.search)
  const hasExplicitUrlState = params.toString().length > 0
  const number = (key: string, fallback: number) => {
    const raw = params.get(key)
    if (raw === null || raw.trim() === '') return fallback
    const value = Number(raw)
    return Number.isFinite(value) ? value : fallback
  }
  const aliases: Record<keyof Filters, string> = {
    search: 'q',
    author: 'author',
    category: 'category',
    min_distance_m: 'dmin',
    max_distance_m: 'dmax',
    min_ascent_m: 'emin',
    max_ascent_m: 'emax',
  }
  const hasUrlBounds = ['w', 's', 'e', 'n'].every((key) => params.has(key))
  const bounds = hasUrlBounds
    ? ([number('w', -180), number('s', -85), number('e', 180), number('n', 85)] as [
        number,
        number,
        number,
        number,
      ])
    : undefined
  return {
    filters: (Object.keys(DEFAULT_FILTERS) as (keyof Filters)[]).reduce(
      (result, key) => ({
        ...result,
        [key]:
          params.get(aliases[key]) ?? (hasExplicitUrlState ? '' : (saved.filters?.[key] ?? '')),
      }),
      { ...DEFAULT_FILTERS },
    ),
    view: {
      longitude: number('lng', DEFAULT_VIEW.longitude),
      latitude: number('lat', DEFAULT_VIEW.latitude),
      zoom: number('z', DEFAULT_VIEW.zoom),
      bounds,
    },
    routeId: params.get('route') ?? (hasExplicitUrlState ? null : (saved.routeId ?? null)),
    viewportOnly:
      params.get('inview') === '1' || (!hasExplicitUrlState && saved.viewportOnly === true),
  }
}

export function writeUrl(
  filters: Filters,
  view: ViewState,
  routeId: string | null,
  mode: 'push' | 'replace',
  viewportOnly = false,
  routeSlug?: string | null,
) {
  const params = new URLSearchParams()
  const aliases: Record<keyof Filters, string> = {
    search: 'q',
    author: 'author',
    category: 'category',
    min_distance_m: 'dmin',
    max_distance_m: 'dmax',
    min_ascent_m: 'emin',
    max_ascent_m: 'emax',
  }
  ;(Object.keys(aliases) as (keyof Filters)[]).forEach((key) => {
    if (filters[key]) params.set(aliases[key], filters[key])
  })
  params.set('lng', view.longitude.toFixed(4))
  params.set('lat', view.latitude.toFixed(4))
  params.set('z', view.zoom.toFixed(2))
  if (view.bounds) {
    params.set('w', view.bounds[0].toFixed(4))
    params.set('s', view.bounds[1].toFixed(4))
    params.set('e', view.bounds[2].toFixed(4))
    params.set('n', view.bounds[3].toFixed(4))
  }
  if (routeId) params.set('route', routeId)
  if (routeId && routeSlug) params.set('slug', routeSlug)
  if (viewportOnly) params.set('inview', '1')
  const query = params.toString()
  const url = `${window.location.pathname}${query ? `?${query}` : ''}`
  window.history[mode === 'push' ? 'pushState' : 'replaceState'](
    { filters, view, routeId },
    '',
    url,
  )
}
