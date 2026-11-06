export type InteractionMode = 'keyboard' | 'pointer' | 'programmatic'

/** A click's `detail` is 0 for keyboard activation. */
export function interactionFromClick(detail: number): InteractionMode {
  return detail === 0 ? 'keyboard' : 'pointer'
}
