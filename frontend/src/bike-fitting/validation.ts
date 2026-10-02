export const MAX_VIDEO_BYTES = 200 * 1024 * 1024
export const MAX_VIDEO_SECONDS = 60
export const RECOMMENDED_MIN_SECONDS = 10
export const RECOMMENDED_MAX_SECONDS = 30

export type FileError = 'empty' | 'large' | 'type' | 'duration' | 'decode'

export function validateFile(file: File): FileError | null {
  if (!file.size) return 'empty'
  if (file.size > MAX_VIDEO_BYTES) return 'large'
  if (!file.type.startsWith('video/')) return 'type'
  return null
}

export function validateDuration(duration: number): FileError | null {
  if (!Number.isFinite(duration) || duration <= 0) return 'decode'
  if (duration > MAX_VIDEO_SECONDS) return 'duration'
  return null
}

export function resizeDimensions(width: number, height: number, maxEdge = 720) {
  const scale = Math.min(1, maxEdge / Math.max(width, height))
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  }
}
