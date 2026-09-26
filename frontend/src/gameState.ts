const MAP_RESET_EVENT = 'bikemapy:game-map-reset'
const DATA_REFRESH_EVENT = 'bikemapy:game-data-refresh'

type GameEvent = Event & { detail?: { reason?: string } }

export function notifyGameMapReset(reason: string): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(MAP_RESET_EVENT, { detail: { reason } }))
  }
}

export function notifyGameDataRefresh(): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(DATA_REFRESH_EVENT))
}

export function subscribeToGameMapReset(listener: (event: GameEvent) => void): () => void {
  if (typeof window === 'undefined') return () => undefined
  window.addEventListener(MAP_RESET_EVENT, listener as EventListener)
  return () => window.removeEventListener(MAP_RESET_EVENT, listener as EventListener)
}

export function subscribeToGameDataRefresh(listener: () => void): () => void {
  if (typeof window === 'undefined') return () => undefined
  window.addEventListener(DATA_REFRESH_EVENT, listener)
  return () => window.removeEventListener(DATA_REFRESH_EVENT, listener)
}
