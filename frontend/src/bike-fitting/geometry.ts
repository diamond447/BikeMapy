export type Landmark = { x: number; y: number; visibility?: number; presence?: number }
export type BikeSide = 'left' | 'right'
export type Point = { x: number; y: number }
export type Angles = {
  ankle: number | null
  knee: number | null
  hip: number | null
  elbow: number | null
  torso: number | null
}

const sideIndices = {
  left: { shoulder: 11, elbow: 13, wrist: 15, hip: 23, knee: 25, ankle: 27, heel: 29, toe: 31 },
  right: { shoulder: 12, elbow: 14, wrist: 16, hip: 24, knee: 26, ankle: 28, heel: 30, toe: 32 },
} as const

export const MIN_VISIBILITY = 0.55

function isConfident(landmark: Landmark | undefined): landmark is Landmark {
  return (
    !!landmark &&
    Number.isFinite(landmark.x) &&
    Number.isFinite(landmark.y) &&
    (landmark.visibility ?? 0) >= MIN_VISIBILITY &&
    (landmark.presence === undefined || landmark.presence >= MIN_VISIBILITY)
  )
}

export function isLandmarkReliable(landmark: Landmark | undefined): landmark is Landmark {
  return isConfident(landmark)
}

export function landmarkPoint(landmark: Landmark, width: number, height: number): Point {
  return { x: landmark.x * width, y: landmark.y * height }
}

export function jointAngle(a: Point, joint: Point, c: Point): number | null {
  const ax = a.x - joint.x
  const ay = a.y - joint.y
  const cx = c.x - joint.x
  const cy = c.y - joint.y
  const divisor = Math.hypot(ax, ay) * Math.hypot(cx, cy)
  if (!Number.isFinite(divisor) || divisor === 0) return null
  const cosine = Math.max(-1, Math.min(1, (ax * cx + ay * cy) / divisor))
  return (Math.acos(cosine) * 180) / Math.PI
}

export function calculateAngles(
  landmarks: Landmark[],
  width: number,
  height: number,
  side: BikeSide,
): Angles | null {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null
  const ids = sideIndices[side]
  const point = (id: number) =>
    isConfident(landmarks[id]) ? landmarkPoint(landmarks[id]!, width, height) : null
  const shoulder = point(ids.shoulder)
  const elbow = point(ids.elbow)
  const wrist = point(ids.wrist)
  const hip = point(ids.hip)
  const knee = point(ids.knee)
  const ankle = point(ids.ankle)
  const heel = point(ids.heel)
  const toe = point(ids.toe)
  const footAxis =
    heel && toe && ankle ? { x: ankle.x + toe.x - heel.x, y: ankle.y + toe.y - heel.y } : null
  const angle = (a: Point | null, joint: Point | null, c: Point | null) =>
    a && joint && c ? jointAngle(a, joint, c) : null
  const torso =
    shoulder && hip
      ? (Math.atan2(Math.abs(hip.y - shoulder.y), Math.abs(shoulder.x - hip.x)) * 180) / Math.PI
      : null
  return {
    ankle: angle(knee, ankle, footAxis),
    knee: angle(hip, knee, ankle),
    hip: angle(shoulder, hip, knee),
    elbow: angle(shoulder, elbow, wrist),
    torso,
  }
}

export function isReliable(landmarks: Landmark[], side: BikeSide = 'left'): boolean {
  const ids = sideIndices[side]
  return [ids.shoulder, ids.elbow, ids.wrist, ids.hip, ids.knee, ids.ankle].every((index) =>
    isConfident(landmarks[index]),
  )
}

export function containRect(
  containerWidth: number,
  containerHeight: number,
  videoWidth: number,
  videoHeight: number,
) {
  if (!containerWidth || !containerHeight || !videoWidth || !videoHeight) return null
  const scale = Math.min(containerWidth / videoWidth, containerHeight / videoHeight)
  const width = videoWidth * scale
  const height = videoHeight * scale
  return { x: (containerWidth - width) / 2, y: (containerHeight - height) / 2, width, height }
}

export function countPedalCycles(
  samples: Array<{ time: number; landmarks: Landmark[] }>,
  side: BikeSide,
): number {
  const ankleIndex = side === 'left' ? 27 : 28
  const points: Array<{ time: number; x: number; y: number }> = []
  for (const sample of samples) {
    const ankle = sample.landmarks[ankleIndex]
    if (isConfident(ankle)) points.push({ time: sample.time, x: ankle.x, y: ankle.y })
  }
  if (points.length < 4) return 0
  const minX = Math.min(...points.map((point) => point.x))
  const maxX = Math.max(...points.map((point) => point.x))
  const minY = Math.min(...points.map((point) => point.y))
  const maxY = Math.max(...points.map((point) => point.y))
  if (maxX - minX < 0.035 || maxY - minY < 0.035) return 0
  const centerX = (minX + maxX) / 2
  const centerY = (minY + maxY) / 2
  let revolutions = 0
  let previous: number | undefined
  let winding = 0
  let previousTime: number | undefined
  for (const point of points) {
    if (previousTime !== undefined && point.time - previousTime > 0.3) {
      previous = undefined
      winding = 0
    }
    const phase = Math.atan2(point.y - centerY, point.x - centerX)
    if (previous === undefined) {
      previous = phase
      previousTime = point.time
      continue
    }
    let delta = phase - previous
    if (delta > Math.PI) delta -= Math.PI * 2
    if (delta < -Math.PI) delta += Math.PI * 2
    winding += delta
    previous = phase
    previousTime = point.time
    if (Math.abs(winding) >= Math.PI * 2 - 0.02) {
      revolutions += 1
      winding += winding > 0 ? -Math.PI * 2 : Math.PI * 2
    }
  }
  return revolutions
}
