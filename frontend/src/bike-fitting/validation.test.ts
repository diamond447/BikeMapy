import { describe, expect, it } from 'vitest'
import {
  MAX_VIDEO_BYTES,
  MAX_VIDEO_SECONDS,
  resizeDimensions,
  validateDuration,
  validateFile,
} from './validation'

describe('bike fitting input limits', () => {
  it('accepts a supported video within the byte limit', () => {
    expect(validateFile(new File(['clip'], 'ride.mp4', { type: 'video/mp4' }))).toBeNull()
    expect(MAX_VIDEO_BYTES).toBe(200 * 1024 * 1024)
  })

  it.each([
    [new File([], 'empty.mp4', { type: 'video/mp4' }), 'empty'],
    [new File(['x'], 'photo.png', { type: 'image/png' }), 'type'],
    [new File(['x'], 'large.mp4', { type: 'video/mp4' }), 'large'],
  ])('validates file %s', (file, expected) => {
    if (file.name === 'large.mp4')
      Object.defineProperty(file, 'size', { value: MAX_VIDEO_BYTES + 1 })
    expect(validateFile(file)).toBe(expected)
  })

  it('rejects videos over 60 seconds and unusable metadata', () => {
    expect(validateDuration(MAX_VIDEO_SECONDS)).toBeNull()
    expect(validateDuration(MAX_VIDEO_SECONDS + 0.01)).toBe('duration')
    expect(validateDuration(Number.NaN)).toBe('decode')
  })

  it('caps decoded frames at a 720 pixel longest edge without upscaling', () => {
    expect(resizeDimensions(1920, 1080)).toEqual({ width: 720, height: 405 })
    expect(resizeDimensions(320, 240)).toEqual({ width: 320, height: 240 })
  })
})
