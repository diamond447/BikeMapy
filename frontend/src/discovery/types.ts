import type { components } from '../api/generated/schema'

export type RouteRecord = components['schemas']['Route']
export type RouteSummary = components['schemas']['RouteSummary']
/** The list summary, plus the detail fields once the selected route's detail loads. */
export type Route = RouteSummary & Partial<Omit<RouteRecord, keyof RouteSummary>>
export type SpatialRoute = components['schemas']['SpatialRoute']
export type ViewportResponse = components['schemas']['ViewportResponse']
export type Geometry = NonNullable<SpatialRoute['geometry']>

export type Filters = {
  search: string
  author: string
  category: string
  min_distance_m: string
  max_distance_m: string
  min_ascent_m: string
  max_ascent_m: string
}

export type ViewState = {
  longitude: number
  latitude: number
  zoom: number
  bounds?: [number, number, number, number]
}

export type DiscoveryState = {
  filters: Filters
  view: ViewState
  routeId: string | null
  viewportOnly: boolean
}
