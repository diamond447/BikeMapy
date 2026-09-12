import { beforeEach, describe, expect, it } from 'vitest'
import { parseState, writeUrl } from './state'

describe('discovery URL state', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState({}, '', '/')
  })

  it('hydrates filters, viewport, and selected route from a shared URL', () => {
    window.history.replaceState(
      {},
      '',
      '/?q=forest&author=Jana&lng=17.1&lat=49.3&z=11&route=route-1',
    )
    const state = parseState()
    expect(state.filters.search).toBe('forest')
    expect(state.filters.author).toBe('Jana')
    expect(state.view.zoom).toBe(11)
    expect(state.routeId).toBe('route-1')
  })

  it('uses defaults for missing URL values and restores private state only without URL state', () => {
    localStorage.setItem(
      'bikemapy:discovery-state',
      JSON.stringify({
        filters: { search: 'private' },
        routeId: 'route-1',
        view: { longitude: 1 },
      }),
    )
    window.history.replaceState({}, '', '/?q=shared')
    const shared = parseState()
    expect(shared.filters.search).toBe('shared')
    expect(shared.routeId).toBeNull()
    expect(shared.view).toEqual(
      expect.objectContaining({ longitude: 16.6, latitude: 49.2, zoom: 7.5 }),
    )
    window.history.replaceState({}, '', '/')
    expect(parseState().filters.search).toBe('private')
  })

  it('writes canonical filter, viewport, and route parameters', () => {
    writeUrl(
      {
        search: 'forest',
        author: '',
        category: '',
        min_distance_m: '',
        max_distance_m: '',
        min_ascent_m: '',
        max_ascent_m: '',
      },
      { longitude: 16.6, latitude: 49.2, zoom: 7.5 },
      'route-1',
      'replace',
      true,
      'forest-loop',
    )
    expect(window.location.search).toContain('q=forest')
    expect(window.location.search).toContain('route=route-1')
    expect(window.location.search).toContain('slug=forest-loop')
    expect(window.location.search).toContain('inview=1')
  })
})
