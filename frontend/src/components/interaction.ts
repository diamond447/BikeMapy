export type InteractionMode = 'keyboard' | 'pointer' | 'programmatic'

/**
 * Native controls report detail 0 for keyboard activation and a positive
 * click count for pointer activation. Keeping this at the event boundary
 * avoids treating every route selection as keyboard navigation.
 */
export function interactionFromClick(detail: number): InteractionMode {
  return detail === 0 ? 'keyboard' : 'pointer'
}
