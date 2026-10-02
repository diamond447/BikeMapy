import { FilesetResolver, PoseLandmarker } from '@mediapipe/tasks-vision'
import type { Landmark } from './geometry'

type Request =
  | { type: 'init' }
  | { type: 'frame'; id: number; bitmap: ImageBitmap; timestamp: number }
  | { type: 'dispose' }

let landmarker: PoseLandmarker | undefined
let lastTimestamp = -1
let messageQueue = Promise.resolve()

async function handleRequest(request: Request) {
  try {
    if (request.type === 'init') {
      const fileset = await FilesetResolver.forVisionTasks(
        '/bike-fitting-assets/mediapipe-1.0.1/wasm',
        true,
      )
      // MediaPipe 1.0.1's module loader expects its glue module to set the
      // global ModuleFactory. In a module worker it otherwise tries DOM script injection.
      const loaderUrl = new URL(fileset.wasmLoaderPath, self.location.href).href
      await import(/* @vite-ignore */ loaderUrl)
      landmarker = await PoseLandmarker.createFromOptions(
        { ...fileset, wasmLoaderPath: '' },
        {
          baseOptions: { modelAssetPath: '/bike-fitting-assets/pose_landmarker_lite-v1.task' },
          runningMode: 'VIDEO',
          numPoses: 1,
          minPoseDetectionConfidence: 0.55,
          minPosePresenceConfidence: 0.55,
          minTrackingConfidence: 0.55,
          outputSegmentationMasks: false,
        },
      )
      self.postMessage({ type: 'ready' })
      return
    }
    if (request.type === 'dispose') {
      landmarker?.close()
      landmarker = undefined
      self.postMessage({ type: 'disposed' })
      self.close()
      return
    }
    if (!landmarker) throw new Error('The pose model is not ready.')
    if (request.timestamp <= lastTimestamp)
      throw new Error('Frame timestamps must increase after seeking.')
    lastTimestamp = request.timestamp
    try {
      const result = landmarker.detectForVideo(request.bitmap, request.timestamp)
      try {
        const landmarks: Landmark[] = (result.landmarks[0] ?? []).map(({ x, y, visibility }) => ({
          x,
          y,
          visibility,
        }))
        self.postMessage({ type: 'frame', id: request.id, timestamp: request.timestamp, landmarks })
      } finally {
        result.close()
      }
    } finally {
      request.bitmap.close()
    }
  } catch (error) {
    if (request.type === 'frame') request.bitmap.close()
    self.postMessage({
      type: 'error',
      id: request.type === 'frame' ? request.id : undefined,
      message: error instanceof Error ? error.message : 'Pose tracking failed.',
    })
  }
}

self.onmessage = (event: MessageEvent<Request>) => {
  messageQueue = messageQueue.then(() => handleRequest(event.data))
}
