import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Landmark } from './geometry'

type WorkerMessage = { type: string; id?: number; timestamp?: number; landmarks?: Landmark[] }

class TestWorker {
  static instances: TestWorker[] = []
  static autoDisposeAcknowledgement = true
  listeners = new Map<string, Set<(event: { data?: WorkerMessage; message?: string }) => void>>()
  postMessage = vi.fn((message: WorkerMessage) => {
    if (message.type === 'init' && TestWorker.readyOnInit) {
      queueMicrotask(() => this.emit({ type: 'ready' }))
    } else if (message.type === 'frame' && TestWorker.answerFrames) {
      const landmarks = TestWorker.poseForFrame(message.timestamp ?? 0)
      queueMicrotask(() =>
        this.emit({ type: 'frame', id: message.id, timestamp: message.timestamp, landmarks }),
      )
    } else if (message.type === 'dispose' && this.autoDisposeAcknowledgement) {
      queueMicrotask(() => this.emit({ type: 'disposed' }))
    }
  })
  terminate = vi.fn()

  static readyOnInit = true
  static answerFrames = true
  static poseForFrame: (timestamp: number) => Landmark[] = () => makePose()
  autoDisposeAcknowledgement = TestWorker.autoDisposeAcknowledgement

  constructor() {
    TestWorker.instances.push(this)
  }

  addEventListener(
    type: string,
    listener: (event: { data?: WorkerMessage; message?: string }) => void,
  ) {
    const listeners = this.listeners.get(type) ?? new Set()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  emit(data: WorkerMessage) {
    this.listeners.get('message')?.forEach((listener) => listener({ data }))
  }

  emitError(message: string) {
    this.listeners.get('error')?.forEach((listener) => listener({ message }))
  }
}

function makePose(): Landmark[] {
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
  // Deliberately omit presence: MediaPipe's optional presence field is not required.
  points[12] = { x: 0.65, y: 0.2, visibility: 0.95 }
  points[14] = { x: 0.52, y: 0.3, visibility: 0.95 }
  points[16] = { x: 0.4, y: 0.32, visibility: 0.95 }
  points[24] = { x: 0.57, y: 0.52, visibility: 0.95 }
  points[26] = { x: 0.44, y: 0.65, visibility: 0.95 }
  points[28] = { x: 0.72, y: 0.79, visibility: 0.95 }
  points[30] = { x: 0.77, y: 0.8, visibility: 0.95 }
  points[32] = { x: 0.67, y: 0.79, visibility: 0.95 }
  return points
}

let bitmapCount = 0

function installVideoMocks() {
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => 'blob:bike-fitting-test'),
  })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(async () => ({ bitmap: ++bitmapCount })),
  )
  vi.stubGlobal('Worker', TestWorker)
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined)
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => undefined)
}

function readyVideo(video: HTMLVideoElement, duration = 0.11, autoSeek = true) {
  let time = 0
  const mockedVideo = video as HTMLVideoElement & { autoSeek: boolean; finishSeek: () => void }
  mockedVideo.autoSeek = autoSeek
  mockedVideo.finishSeek = () => video.dispatchEvent(new Event('seeked'))
  Object.defineProperties(video, {
    readyState: { configurable: true, value: 2 },
    duration: { configurable: true, value: duration },
    videoWidth: { configurable: true, value: 1280 },
    videoHeight: { configurable: true, value: 720 },
    currentTime: {
      configurable: true,
      get: () => time,
      set: (value: number) => {
        time = value
        if (mockedVideo.autoSeek) queueMicrotask(mockedVideo.finishSeek)
      },
    },
  })
}

async function getEnabledApp() {
  vi.stubEnv('VITE_ENABLE_BIKE_FITTING', 'true')
  vi.resetModules()
  return (await import('./BikeFittingApp')).default
}

async function chooseValidVideo(duration = 0.11) {
  const file = new File(['small video payload'], 'ride.mp4', { type: 'video/mp4' })
  fireEvent.change(screen.getByLabelText(/choose video/i), { target: { files: [file] } })
  await screen.findByText(/ride\.mp4/)
  const videos = Array.from(document.querySelectorAll('video'))
  expect(videos).toHaveLength(2)
  videos.forEach((video) => readyVideo(video, duration))
  fireEvent.loadedMetadata(videos[0]!)
  fireEvent.loadedMetadata(videos[1]!)
  return { file, videos }
}

async function startAnalysis() {
  fireEvent.click(screen.getByRole('button', { name: /analyse video/i }))
}

beforeEach(() => {
  localStorage.clear()
  TestWorker.instances = []
  TestWorker.readyOnInit = true
  TestWorker.answerFrames = true
  TestWorker.autoDisposeAcknowledgement = true
  TestWorker.poseForFrame = () => makePose()
  bitmapCount = 0
  installVideoMocks()
  Object.defineProperty(navigator, 'language', { configurable: true, value: 'en-US' })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('bike fitting app lifecycle', () => {
  it('explains when the feature is disabled and loads the saved or browser language when enabled', async () => {
    vi.stubEnv('VITE_ENABLE_BIKE_FITTING', 'false')
    vi.resetModules()
    const DisabledApp = (await import('./BikeFittingApp')).default
    const { unmount } = render(<DisabledApp />)
    expect(screen.getByRole('heading', { name: 'Bike fitting' })).toBeInTheDocument()
    expect(screen.getByText(/feature is disabled/i)).toBeInTheDocument()
    unmount()

    localStorage.setItem('bikemapy:language', 'cs')
    const EnabledApp = await getEnabledApp()
    render(<EnabledApp />)
    expect(screen.getByRole('heading', { name: 'Nastavení posedu' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /přepnout jazyk na angličtinu/i }))
    expect(screen.getByRole('heading', { name: 'Bike fitting' })).toBeInTheDocument()
    expect(localStorage.getItem('bikemapy:language')).toBe('en')
    cleanup()
    localStorage.clear()
    Object.defineProperty(navigator, 'language', { configurable: true, value: 'cs-CZ' })
    const BrowserLanguageApp = await getEnabledApp()
    render(<BrowserLanguageApp />)
    expect(screen.getByRole('heading', { name: 'Nastavení posedu' })).toBeInTheDocument()
  })

  it('validates upload and metadata, then sequentially analyzes decoded frames with visibility-only landmarks', async () => {
    const App = await getEnabledApp()
    render(<App />)
    expect(document.documentElement.lang).toBe('en')

    const input = screen.getByLabelText(/choose video/i)
    fireEvent.change(input, {
      target: { files: [new File(['not a video'], 'notes.txt', { type: 'text/plain' })] },
    })
    expect(screen.getByRole('alert')).toHaveTextContent(/choose a video file/i)
    // Exercise the size limit without allocating a 200 MB test fixture.
    const oversized = new File(['x'], 'oversized.mp4', { type: 'video/mp4' })
    Object.defineProperty(oversized, 'size', { value: 200 * 1024 * 1024 + 1 })
    fireEvent.change(input, { target: { files: [oversized] } })
    expect(screen.getByRole('alert')).toHaveTextContent(/exceeds the 200 mb limit/i)

    fireEvent.change(input, {
      target: { files: [new File(['video'], 'ride.mp4', { type: 'video/mp4' })] },
    })
    await screen.findByText(/ride\.mp4/)
    const [preview, decoder] = Array.from(document.querySelectorAll('video'))
    readyVideo(preview!, 61)
    readyVideo(decoder!, 61)
    expect(screen.getByRole('button', { name: /analyse video/i })).toBeDisabled()
    await act(async () => {
      fireEvent.loadedMetadata(preview!)
      await Promise.resolve()
    })
    expect(screen.getByRole('alert')).toHaveTextContent(/no longer than 60 seconds/i)

    fireEvent.change(input, {
      target: { files: [new File(['video'], 'ride.mp4', { type: 'video/mp4' })] },
    })
    await screen.findByText(/ride\.mp4/)
    const videos = Array.from(document.querySelectorAll('video'))
    videos.forEach((video) => readyVideo(video, 0.11))
    fireEvent.loadedMetadata(videos[0]!)
    fireEvent.loadedMetadata(videos[1]!)
    TestWorker.poseForFrame = (timestamp) => {
      const points = makePose()
      if (timestamp === 50) points[25] = { ...points[25]!, visibility: 0.1 }
      return points
    }
    await startAnalysis()

    await waitFor(() =>
      expect(screen.getByText(/reliable frames/i)).toHaveTextContent('2 reliable frames'),
    )
    expect(screen.getByText('READY')).toBeInTheDocument()
    const worker = TestWorker.instances.at(-1)!
    const frames = worker.postMessage.mock.calls.filter(([message]) => message.type === 'frame')
    expect(frames).toHaveLength(3)
    expect(frames.map(([message]) => message.timestamp)).toEqual([0, 50, 100])
    expect(createImageBitmap).toHaveBeenCalledTimes(3)
    expect(worker.terminate).toHaveBeenCalledTimes(1)

    const leftKnee = screen.getByText('Knee').nextElementSibling?.textContent
    expect(leftKnee).not.toBe('—')
    fireEvent.change(screen.getByLabelText(/video position/i), { target: { value: '0.05' } })
    await waitFor(() =>
      expect(screen.getByText(/tracking is unreliable for this frame/i)).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Right' }))
    expect(screen.getByRole('button', { name: 'Right' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Knee').nextElementSibling?.textContent).not.toBe('—')
    expect(screen.getByText('Knee').nextElementSibling?.textContent).not.toBe(leftKnee)
    expect(screen.queryByText(/tracking is unreliable for this frame/i)).not.toBeInTheDocument()
  })

  it('releases a worker when a replacement video arrives during inference and ignores its late frame', async () => {
    const App = await getEnabledApp()
    render(<App />)
    const { videos } = await chooseValidVideo(0.11)
    TestWorker.answerFrames = false
    await startAnalysis()
    await waitFor(() =>
      expect(TestWorker.instances[0]?.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame', id: 1 }),
        expect.any(Array),
      ),
    )
    const oldWorker = TestWorker.instances[0]!
    oldWorker.autoDisposeAcknowledgement = false
    // Replacing the clip aborts its unresolved inference and starts a fresh run.
    fireEvent.change(screen.getByLabelText(/choose video/i), {
      target: { files: [new File(['replacement'], 'replacement.mp4', { type: 'video/mp4' })] },
    })
    await screen.findByText(/replacement\.mp4/)
    expect(TestWorker.instances[0]?.postMessage).toHaveBeenCalledWith({ type: 'dispose' })
    const replacements = Array.from(document.querySelectorAll('video'))
    replacements.forEach((video) => readyVideo(video, 0.06))
    fireEvent.loadedMetadata(replacements[0]!)
    fireEvent.loadedMetadata(replacements[1]!)
    TestWorker.autoDisposeAcknowledgement = true
    await startAnalysis()
    const currentWorker = TestWorker.instances[1]!
    await waitFor(() =>
      expect(currentWorker.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame', id: 1 }),
        expect.any(Array),
      ),
    )
    expect(screen.getByRole('button', { name: /cancel analysis/i })).toBeInTheDocument()
    await act(async () => {
      oldWorker.emit({ type: 'frame', id: 1, timestamp: 0, landmarks: makePose() })
      oldWorker.emitError('stale worker failure')
    })
    expect(screen.getByRole('button', { name: /cancel analysis/i })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(
      currentWorker.postMessage.mock.calls.filter(([message]) => message.type === 'frame'),
    ).toHaveLength(1)
    await act(async () =>
      currentWorker.emit({ type: 'frame', id: 1, timestamp: 0, landmarks: makePose() }),
    )
    await waitFor(() =>
      expect(currentWorker.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame', id: 2 }),
        expect.any(Array),
      ),
    )
    await act(async () =>
      currentWorker.emit({ type: 'frame', id: 2, timestamp: 60, landmarks: makePose() }),
    )
    await waitFor(() =>
      expect(screen.getByText(/reliable frames/i)).toHaveTextContent('2 reliable frames'),
    )
    oldWorker.emit({ type: 'disposed' })
    await waitFor(() => expect(oldWorker.terminate).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/replacement\.mp4/)).toBeInTheDocument()
    expect(screen.getByText(/2 reliable frames/i)).toBeInTheDocument()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:bike-fitting-test')
    expect(videos).toHaveLength(2)
  })

  it('cancels a pending inference and allows a fast retry without accepting the old response', async () => {
    const App = await getEnabledApp()
    render(<App />)
    await chooseValidVideo(0.06)
    TestWorker.answerFrames = false
    await startAnalysis()
    await waitFor(() =>
      expect(TestWorker.instances[0]?.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame', id: 1 }),
        expect.any(Array),
      ),
    )
    fireEvent.click(screen.getByRole('button', { name: /cancel analysis/i }))
    expect(TestWorker.instances[0]?.postMessage).toHaveBeenCalledWith({ type: 'dispose' })
    await waitFor(() => expect(TestWorker.instances[0]?.terminate).toHaveBeenCalledTimes(1))

    TestWorker.answerFrames = true
    await startAnalysis()
    await waitFor(() =>
      expect(screen.getByText(/reliable frames/i)).toHaveTextContent('2 reliable frames'),
    )
    await act(async () => {
      TestWorker.instances[0]?.emit({
        type: 'frame',
        id: 1,
        timestamp: 0,
        landmarks: makePose().map((point) => ({ ...point, visibility: 0.01 })),
      })
    })
    expect(screen.getByText(/2 reliable frames/i)).toBeInTheDocument()
    expect(screen.getByText('READY')).toBeInTheDocument()
  })

  it('waits for the decoder seek event before creating a bitmap for that frame', async () => {
    const App = await getEnabledApp()
    render(<App />)
    const { videos } = await chooseValidVideo(0.06)
    const decoder = videos[1] as HTMLVideoElement & { autoSeek: boolean; finishSeek: () => void }
    decoder.autoSeek = false
    decoder.currentTime = 0.02
    decoder.finishSeek()

    await startAnalysis()
    await act(async () => {
      for (let step = 0; step < 8; step += 1) await Promise.resolve()
    })
    expect(screen.getByRole('button', { name: /cancel analysis/i })).toBeInTheDocument()
    expect(createImageBitmap).not.toHaveBeenCalled()

    await act(async () => decoder.finishSeek())
    decoder.autoSeek = true
    await waitFor(() => expect(createImageBitmap).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(TestWorker.instances[0]?.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame', id: 1 }),
        expect.any(Array),
      ),
    )
    fireEvent.click(screen.getByRole('button', { name: /cancel analysis/i }))
  })

  it('invalidates a pending playback seek when its video is replaced or removed', async () => {
    const App = await getEnabledApp()
    render(<App />)
    const first = await chooseValidVideo(0.11)
    const firstPreview = first.videos[0] as HTMLVideoElement & {
      autoSeek: boolean
      finishSeek: () => void
    }
    firstPreview.autoSeek = false
    fireEvent.change(screen.getByLabelText(/video position/i), { target: { value: '0.05' } })

    fireEvent.change(screen.getByLabelText(/choose video/i), {
      target: { files: [new File(['replacement'], 'replacement.mp4', { type: 'video/mp4' })] },
    })
    await screen.findByText(/replacement\.mp4/)
    const replacementVideos = Array.from(document.querySelectorAll('video'))
    replacementVideos.forEach((video) => readyVideo(video, 0.11))
    fireEvent.loadedMetadata(replacementVideos[0]!)
    fireEvent.loadedMetadata(replacementVideos[1]!)
    await act(async () => firstPreview.finishSeek())
    expect(screen.getByLabelText(/video position/i)).toHaveValue('0')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    const replacementPreview = replacementVideos[0] as HTMLVideoElement & {
      autoSeek: boolean
      finishSeek: () => void
    }
    replacementPreview.autoSeek = false
    fireEvent.change(screen.getByLabelText(/video position/i), { target: { value: '0.05' } })
    fireEvent.click(screen.getByRole('button', { name: /remove video/i }))
    await act(async () => replacementPreview.finishSeek())
    expect(screen.queryByLabelText(/video position/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('resets the file chooser so the same clip can be selected again after removal', async () => {
    const App = await getEnabledApp()
    render(<App />)
    const input = screen.getByLabelText(/choose video/i) as HTMLInputElement
    const file = new File(['same clip'], 'same.mp4', { type: 'video/mp4' })
    fireEvent.change(input, { target: { files: [file] } })
    await screen.findByText(/same\.mp4/)
    expect(input.value).toBe('')
    fireEvent.click(screen.getByRole('button', { name: /remove video/i }))
    fireEvent.change(input, { target: { files: [file] } })
    expect(await screen.findByText(/same\.mp4/)).toBeInTheDocument()
  })

  it('ignores a delayed disposed acknowledgement from a completed run after a replacement run starts', async () => {
    const App = await getEnabledApp()
    render(<App />)
    await chooseValidVideo(0.06)
    TestWorker.autoDisposeAcknowledgement = false
    await startAnalysis()
    const oldWorker = TestWorker.instances[0]!
    await waitFor(() => expect(oldWorker.postMessage).toHaveBeenCalledWith({ type: 'dispose' }))
    oldWorker.autoDisposeAcknowledgement = false

    TestWorker.autoDisposeAcknowledgement = true
    TestWorker.answerFrames = false
    fireEvent.change(screen.getByLabelText(/choose video/i), {
      target: { files: [new File(['new clip'], 'new-clip.mp4', { type: 'video/mp4' })] },
    })
    await screen.findByText(/new-clip\.mp4/)
    const videos = Array.from(document.querySelectorAll('video'))
    videos.forEach((video) => readyVideo(video, 0.06))
    fireEvent.loadedMetadata(videos[0]!)
    fireEvent.loadedMetadata(videos[1]!)
    await startAnalysis()
    const currentWorker = TestWorker.instances[1]!
    await waitFor(() =>
      expect(currentWorker.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame', id: 1 }),
        expect.any(Array),
      ),
    )

    await act(async () => oldWorker.emit({ type: 'disposed' }))
    expect(screen.getByRole('button', { name: /cancel analysis/i })).toBeInTheDocument()
    expect(document.querySelector('button.bike-fitting-start')).toBeDisabled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveValue(0)

    await act(async () =>
      currentWorker.emit({ type: 'frame', id: 1, timestamp: 0, landmarks: makePose() }),
    )
    await waitFor(() =>
      expect(currentWorker.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame', id: 2 }),
        expect.any(Array),
      ),
    )
    await act(async () =>
      currentWorker.emit({ type: 'frame', id: 2, timestamp: 60, landmarks: makePose() }),
    )
    await waitFor(() =>
      expect(screen.getByText(/reliable frames/i)).toHaveTextContent('2 reliable frames'),
    )
    expect(screen.getByText(/new-clip\.mp4/)).toBeInTheDocument()
  })

  it('reports a worker error after initialization as a retryable failure', async () => {
    const App = await getEnabledApp()
    render(<App />)
    await chooseValidVideo(0.04)
    TestWorker.answerFrames = false
    await startAnalysis()
    await waitFor(() =>
      expect(TestWorker.instances[0]?.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'frame' }),
        expect.any(Array),
      ),
    )
    act(() => TestWorker.instances[0]?.emitError('worker crashed'))
    expect(await screen.findByRole('alert')).toHaveTextContent(/worker crashed/i)
    expect(screen.getByRole('button', { name: /try again/i })).toBeEnabled()
  })

  it('turns a model initialization timeout into a retryable failure', async () => {
    const App = await getEnabledApp()
    render(<App />)
    await chooseValidVideo(0.04)
    TestWorker.readyOnInit = false
    vi.useFakeTimers()
    await startAnalysis()
    await act(async () => {
      for (let step = 0; step < 8; step += 1) await Promise.resolve()
    })
    expect(TestWorker.instances).toHaveLength(1)
    await act(async () => {
      vi.advanceTimersByTime(30_001)
      await Promise.resolve()
    })
    expect(screen.getByRole('alert')).toHaveTextContent(/model initialization timed out/i)
    expect(screen.getByRole('button', { name: /try again/i })).toBeEnabled()
  })

  it('turns an inference timeout into a retryable failure', async () => {
    const App = await getEnabledApp()
    render(<App />)
    await chooseValidVideo(0.06)
    TestWorker.answerFrames = false
    vi.useFakeTimers()
    await startAnalysis()
    await act(async () => {
      for (let step = 0; step < 8; step += 1) await Promise.resolve()
    })
    const worker = TestWorker.instances[0]!
    expect(worker.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'frame', id: 1 }),
      expect.any(Array),
    )
    await act(async () => {
      vi.advanceTimersByTime(20_001)
      await Promise.resolve()
    })
    expect(screen.getByRole('alert')).toHaveTextContent(/pose inference timed out on this frame/i)
    expect(screen.getByRole('button', { name: /try again/i })).toBeEnabled()
  })
})
