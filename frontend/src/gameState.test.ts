import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  notifyGameDataRefresh,
  notifyGameMapReset,
  subscribeToGameDataRefresh,
  subscribeToGameMapReset,
} from './gameState'

describe('game state invalidation events', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('delivers reset reasons and refreshes to subscribed components', () => {
    const reset = vi.fn()
    const refresh = vi.fn()
    const unsubscribeReset = subscribeToGameMapReset(reset)
    const unsubscribeRefresh = subscribeToGameDataRefresh(refresh)

    notifyGameMapReset('account-deleted')
    notifyGameDataRefresh()

    expect(reset).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { reason: 'account-deleted' } }),
    )
    expect(refresh).toHaveBeenCalledTimes(1)

    unsubscribeReset()
    unsubscribeRefresh()
    notifyGameMapReset('logout')
    notifyGameDataRefresh()
    expect(reset).toHaveBeenCalledTimes(1)
    expect(refresh).toHaveBeenCalledTimes(1)
  })
})
