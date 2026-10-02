import { describe, expect, it } from 'vitest'
import {
  calculateAngles,
  countPedalCycles,
  isReliable,
  jointAngle,
  landmarkPoint,
  type Landmark,
} from './geometry'

function pose(): Landmark[] {
  const points: Landmark[] = Array.from({ length: 33 }, () => ({
    x: 0.5,
    y: 0.5,
    visibility: 0.95,
  }))
  points[11] = { x: 0.35, y: 0.2, visibility: 0.95 }
  points[13] = { x: 0.48, y: 0.3, visibility: 0.95 }
  points[15] = { x: 0.6, y: 0.32, visibility: 0.95 }
  points[23] = { x: 0.43, y: 0.52, visibility: 0.95 }
  points[25] = { x: 0.56, y: 0.65, visibility: 0.95 }
  points[27] = { x: 0.48, y: 0.79, visibility: 0.95 }
  points[29] = { x: 0.43, y: 0.8, visibility: 0.95 }
  points[31] = { x: 0.55, y: 0.79, visibility: 0.95 }
  return points
}

describe('bike fitting geometry', () => {
  it('maps normalized points into non-square image coordinates', () => {
    expect(landmarkPoint({ x: 0.25, y: 0.5 }, 800, 400)).toEqual({ x: 200, y: 200 })
  })

  it('computes 2D knee, hip, elbow, and torso angles', () => {
    const angles = calculateAngles(pose(), 1920, 1080, 'left')
    expect(angles?.knee).toBeGreaterThan(0)
    expect(angles?.hip).toBeGreaterThan(0)
    expect(angles?.elbow).toBeGreaterThan(0)
    expect(angles?.ankle).toBeGreaterThan(0)
    expect(Number.isFinite(angles?.torso)).toBe(true)
  })

  it('keeps torso inclination consistent for mirrored side views', () => {
    const original = calculateAngles(pose(), 1920, 1080, 'left')
    const mirrored = pose()
    for (const [left, right] of [
      [11, 12],
      [13, 14],
      [15, 16],
      [23, 24],
      [25, 26],
      [27, 28],
      [29, 30],
      [31, 32],
    ]) {
      mirrored[right] = { ...mirrored[left]!, x: 1 - mirrored[left]!.x }
    }
    expect(calculateAngles(mirrored, 1920, 1080, 'right')?.torso).toBeCloseTo(original!.torso!)
  })

  it('measures torso inclination from horizontal with 90 degrees upright', () => {
    const upright = pose()
    upright[11] = { ...upright[11]!, x: upright[23]!.x }
    const horizontal = pose()
    horizontal[11] = { ...horizontal[11]!, y: horizontal[23]!.y }
    expect(calculateAngles(upright, 800, 800, 'left')?.torso).toBeCloseTo(90)
    expect(calculateAngles(horizontal, 800, 800, 'left')?.torso).toBeCloseTo(0)
  })

  it('keeps other measurements when foot landmarks are occluded', () => {
    const points = pose()
    points[29] = { ...points[29]!, visibility: 0.1 }
    const angles = calculateAngles(points, 1920, 1080, 'left')
    expect(angles?.ankle).toBeNull()
    expect(angles?.knee).not.toBeNull()
    expect(angles?.hip).not.toBeNull()
  })

  it('uses the heel-to-toe direction as the foot axis at the ankle', () => {
    const points = pose()
    const width = 1920
    const height = 1080
    const ankle = landmarkPoint(points[27]!, width, height)
    const heel = landmarkPoint(points[29]!, width, height)
    const toe = landmarkPoint(points[31]!, width, height)
    const expected = jointAngle(landmarkPoint(points[25]!, width, height), ankle, {
      x: ankle.x + toe.x - heel.x,
      y: ankle.y + toe.y - heel.y,
    })
    expect(calculateAngles(points, width, height, 'left')?.ankle).toBeCloseTo(expected!)
  })

  it('uses SDK visibility fields and rejects invalid coordinates or low confidence', () => {
    const points = pose()
    expect(isReliable(points, 'left')).toBe(true)
    points[25] = { ...points[25]!, x: Number.NaN }
    expect(isReliable(points, 'left')).toBe(false)
    expect(calculateAngles(points, 800, 400, 'left')?.knee).toBeNull()
    points[25] = { ...points[25]!, x: 0.5, visibility: 0.1 }
    expect(isReliable(points, 'left')).toBe(false)
    points[25] = { ...points[25]!, visibility: 0.95, presence: 0.1 }
    expect(isReliable(points, 'left')).toBe(false)
  })

  it('counts complete ankle revolutions around the observed ankle path, below the hip', () => {
    const samples = Array.from({ length: 5 * 40 + 1 }, (_, index) => {
      const phase = (index / 40) * Math.PI * 2
      const points = pose()
      points[27] = {
        x: 0.48 + 0.08 * Math.cos(phase),
        y: 0.78 + 0.13 * Math.sin(phase),
        visibility: 0.95,
      }
      return { time: index / 20, landmarks: points }
    })
    expect(countPedalCycles(samples, 'left')).toBe(5)
  })

  it('breaks pedal-cycle winding across a long tracking dropout', () => {
    const samples = Array.from({ length: 161 }, (_, index) => {
      const time = index < 80 ? index / 20 : 8 + (index - 80) / 20
      const phase = (index / 40) * Math.PI * 2
      const points = pose()
      points[27] = {
        x: 0.5 + 0.08 * Math.cos(phase),
        y: 0.8 + 0.12 * Math.sin(phase),
        visibility: 0.95,
      }
      return { time, landmarks: points }
    })
    expect(countPedalCycles(samples, 'left')).toBe(3)
  })
})
