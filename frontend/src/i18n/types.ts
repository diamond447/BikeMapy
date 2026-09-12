export type Language = 'en' | 'cs'

/** Shared translation contract used by feature components. */
import { translations } from './translations'

export type Copy = (typeof translations)[Language]
