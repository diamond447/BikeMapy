import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { BikeSide, Landmark } from './geometry'
import {
  calculateAngles,
  containRect,
  countPedalCycles,
  isLandmarkReliable,
  isReliable,
} from './geometry'
import {
  MAX_VIDEO_BYTES,
  MAX_VIDEO_SECONDS,
  RECOMMENDED_MAX_SECONDS,
  RECOMMENDED_MIN_SECONDS,
  resizeDimensions,
  validateDuration,
  validateFile,
  type FileError,
} from './validation'

type Language = 'cs' | 'en'
type Sample = { time: number; landmarks: Landmark[]; aliased: boolean }
type WorkerMessage =
  | { type: 'ready' }
  | { type: 'frame'; id: number; timestamp: number; landmarks: Landmark[] }
  | { type: 'error'; id?: number; message: string }
  | { type: 'disposed' }
type Status = 'idle' | 'loading' | 'analyzing' | 'ready' | 'failed'

const enabled = import.meta.env.VITE_ENABLE_BIKE_FITTING === 'true'
const LANGUAGE_KEY = 'bikemapy:language'
const MAX_SAMPLES_PER_SECOND = 20
const fileErrors: Record<FileError, [string, string]> = {
  empty: ['Soubor je prázdný. Vyberte jiné video.', 'The file is empty. Choose another video.'],
  large: ['Video je větší než limit 200 MB.', 'The video exceeds the 200 MB limit.'],
  type: ['Vyberte video soubor.', 'Choose a video file.'],
  duration: ['Video může mít nejvýše 60 sekund.', 'The video must be no longer than 60 seconds.'],
  decode: [
    'Prohlížeč nedokáže přečíst délku videa. Zkuste běžný MP4 nebo WebM soubor.',
    'The browser could not read the video duration. Try a standard MP4 or WebM file.',
  ],
}

function initialLanguage(): Language {
  try {
    const saved = localStorage.getItem(LANGUAGE_KEY)
    if (saved === 'cs' || saved === 'en') return saved
  } catch {
    /* private browsing */
  }
  return navigator.language.toLowerCase().startsWith('cs') ? 'cs' : 'en'
}

function messages(cs: string, en: string, language: Language) {
  return language === 'cs' ? cs : en
}

function waitForSeek(video: HTMLVideoElement, time: number, signal: AbortSignal): Promise<void> {
  const alreadyAtTarget = Math.abs(video.currentTime - time) < 0.001
  if (alreadyAtTarget && video.readyState >= 2) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => finish(new Error('Video seek timed out.')), 5000)
    const cleanup = () => {
      window.clearTimeout(timeout)
      video.removeEventListener('seeked', done)
      video.removeEventListener('loadeddata', done)
      video.removeEventListener('error', failed)
      signal.removeEventListener('abort', aborted)
    }
    const done = () => {
      cleanup()
      resolve()
    }
    const failed = () => {
      cleanup()
      reject(new Error('This video frame could not be decoded.'))
    }
    const aborted = () => finish(new Error('Analysis cancelled.'))
    video.addEventListener('seeked', done, { once: true })
    if (alreadyAtTarget) video.addEventListener('loadeddata', done, { once: true })
    video.addEventListener('error', failed, { once: true })
    signal.addEventListener('abort', aborted, { once: true })
    const finish = (error: Error) => {
      cleanup()
      reject(error)
    }
    try {
      video.currentTime = time
      if (alreadyAtTarget && video.readyState >= 2) done()
    } catch (error) {
      finish(error instanceof Error ? error : new Error('Video seek failed.'))
    }
  })
}

function waitForMetadata(video: HTMLVideoElement, signal: AbortSignal): Promise<void> {
  if (video.readyState >= 1) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => finish(new Error('Video metadata timed out.')), 10_000)
    const cleanup = () => {
      window.clearTimeout(timeout)
      video.removeEventListener('loadedmetadata', loaded)
      video.removeEventListener('error', failed)
      signal.removeEventListener('abort', aborted)
    }
    const loaded = () => {
      cleanup()
      resolve()
    }
    const failed = () => {
      cleanup()
      reject(new Error('The browser could not decode this video.'))
    }
    const aborted = () => finish(new Error('Analysis cancelled.'))
    const finish = (error: Error) => {
      cleanup()
      reject(error)
    }
    video.addEventListener('loadedmetadata', loaded, { once: true })
    video.addEventListener('error', failed, { once: true })
    signal.addEventListener('abort', aborted, { once: true })
  })
}

function withTimeout<T>(
  promise: Promise<T>,
  duration: number,
  message: string,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      window.clearTimeout(timeout)
      signal?.removeEventListener('abort', aborted)
    }
    const timeout = window.setTimeout(() => {
      cleanup()
      reject(new Error(message))
    }, duration)
    const aborted = () => {
      cleanup()
      reject(new Error('Analysis cancelled.'))
    }
    signal?.addEventListener('abort', aborted, { once: true })
    promise.then(
      (value) => {
        cleanup()
        resolve(value)
      },
      (error: unknown) => {
        cleanup()
        reject(error)
      },
    )
  })
}

export default function BikeFittingApp() {
  const videoRef = useRef<HTMLVideoElement>(null)
  const decoderRef = useRef<HTMLVideoElement>(null)
  const frameHostRef = useRef<HTMLDivElement>(null)
  const workerRef = useRef<Worker | null>(null)
  const pendingRef = useRef(
    new Map<number, { resolve: (landmarks: Landmark[]) => void; reject: (error: Error) => void }>(),
  )
  const requestIdRef = useRef(0)
  const objectUrlRef = useRef<string | null>(null)
  const runGenerationRef = useRef(0)
  const runAbortRef = useRef<AbortController | null>(null)
  const playerSeekAbortRef = useRef<AbortController | null>(null)
  const playerSeekGenerationRef = useRef(0)
  const samplesRef = useRef<Sample[]>([])
  const [language, setLanguage] = useState<Language>(initialLanguage)
  const [file, setFile] = useState<File | null>(null)
  const [videoUrl, setVideoUrl] = useState<string | null>(null)
  const [duration, setDuration] = useState<number | null>(null)
  const [side, setSide] = useState<BikeSide>('left')
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<[string, string] | null>(null)
  const [samples, setSamples] = useState<Sample[]>([])
  const [progress, setProgress] = useState(0)
  const [videoSize, setVideoSize] = useState({ width: 0, height: 0 })
  const [videoTime, setVideoTime] = useState(0)
  const [seekError, setSeekError] = useState(false)
  const [isPlaying, setIsPlaying] = useState(false)
  const [hostSize, setHostSize] = useState({ width: 0, height: 0 })

  useEffect(() => {
    try {
      localStorage.setItem(LANGUAGE_KEY, language)
    } catch {
      /* private browsing */
    }
    document.documentElement.lang = language
  }, [language])

  const stopAnalysis = useCallback(() => {
    runGenerationRef.current += 1
    runAbortRef.current?.abort()
    runAbortRef.current = null
    playerSeekAbortRef.current?.abort()
    playerSeekAbortRef.current = null
    playerSeekGenerationRef.current += 1
    const worker = workerRef.current
    workerRef.current = null
    for (const pending of pendingRef.current.values())
      pending.reject(new Error('Analysis cancelled.'))
    pendingRef.current.clear()
    if (worker) {
      const timeout = window.setTimeout(() => worker.terminate(), 500)
      worker.addEventListener('message', (event: MessageEvent<WorkerMessage>) => {
        if (event.data.type === 'disposed') {
          window.clearTimeout(timeout)
          worker.terminate()
        }
      })
      worker.postMessage({ type: 'dispose' })
    }
  }, [])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !isPlaying) return
    let active = true
    let frameRequest: number | null = null
    let frameCallback = false
    const update = (_now: number, metadata?: VideoFrameCallbackMetadata) => {
      if (!active) return
      setVideoTime(metadata?.mediaTime ?? video.currentTime)
      frameCallback = typeof video.requestVideoFrameCallback === 'function'
      frameRequest = frameCallback
        ? video.requestVideoFrameCallback!(update)
        : window.requestAnimationFrame((time) => update(time))
    }
    frameCallback = typeof video.requestVideoFrameCallback === 'function'
    frameRequest = frameCallback
      ? video.requestVideoFrameCallback!(update)
      : window.requestAnimationFrame((time) => update(time))
    return () => {
      active = false
      if (frameRequest !== null) {
        if (frameCallback) video.cancelVideoFrameCallback?.(frameRequest)
        else window.cancelAnimationFrame(frameRequest)
      }
    }
  }, [isPlaying])

  const clearFile = useCallback(() => {
    stopAnalysis()
    videoRef.current?.pause()
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    objectUrlRef.current = null
    setVideoUrl(null)
    setFile(null)
    setDuration(null)
    setVideoSize({ width: 0, height: 0 })
    setVideoTime(0)
    samplesRef.current = []
    setSamples([])
    setProgress(0)
    setError(null)
    setStatus('idle')
  }, [stopAnalysis])

  useEffect(
    () => () => {
      stopAnalysis()
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    },
    [stopAnalysis],
  )

  useEffect(() => {
    const host = frameHostRef.current
    if (!host || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setHostSize({ width: entry.contentRect.width, height: entry.contentRect.height })
    })
    observer.observe(host)
    return () => observer.disconnect()
  }, [])

  const onFileChange = (candidate?: File) => {
    clearFile()
    if (!candidate) return
    const issue = validateFile(candidate)
    if (issue) {
      setError(fileErrors[issue])
      setStatus('failed')
      return
    }
    const url = URL.createObjectURL(candidate)
    objectUrlRef.current = url
    setVideoUrl(url)
    setFile(candidate)
    setStatus('idle')
  }

  const onMetadata = () => {
    const video = videoRef.current
    if (!video) return
    const issue = validateDuration(video.duration)
    if (issue) {
      setError(fileErrors[issue])
      setStatus('failed')
      return
    }
    setDuration(video.duration)
    setVideoSize({ width: video.videoWidth, height: video.videoHeight })
  }

  const failAnalysis = (cause: unknown, runGeneration: number) => {
    if (runGeneration !== runGenerationRef.current) return
    const reason = cause instanceof Error ? cause.message : 'Pose tracking failed.'
    setError([`Analýza selhala: ${reason}`, `Analysis failed: ${reason}`])
    setStatus('failed')
    stopAnalysis()
  }

  const startAnalysis = async () => {
    if (!file || !videoUrl || duration === null) return
    stopAnalysis()
    const runGeneration = runGenerationRef.current
    const abortController = new AbortController()
    runAbortRef.current = abortController
    const isCurrentRun = () =>
      runGeneration === runGenerationRef.current && !abortController.signal.aborted
    samplesRef.current = []
    setSamples([])
    setProgress(0)
    setError(null)
    setSeekError(false)
    requestIdRef.current = 0
    const runPending = new Map<
      number,
      { resolve: (landmarks: Landmark[]) => void; reject: (error: Error) => void }
    >()
    pendingRef.current = runPending
    setStatus('loading')
    const worker = new Worker(new URL('./pose.worker.ts', import.meta.url), { type: 'module' })
    workerRef.current = worker
    let readyResolve: () => void = () => undefined
    let readyReject: (error: Error) => void = () => undefined
    const waitUntilReady = new Promise<void>((resolve, reject) => {
      readyResolve = resolve
      readyReject = (error) => reject(error)
    })
    worker.addEventListener('message', (event: MessageEvent<WorkerMessage>) => {
      if (runGeneration !== runGenerationRef.current || workerRef.current !== worker) return
      const message = event.data
      if (message.type === 'ready') readyResolve()
      else if (message.type === 'error') {
        const error = new Error(message.message)
        if (message.id !== undefined) {
          const pending = runPending.get(message.id)
          pending?.reject(error)
          runPending.delete(message.id)
        } else {
          readyReject(error)
          for (const pending of runPending.values()) pending.reject(error)
          runPending.clear()
          if (runGeneration === runGenerationRef.current && workerRef.current === worker)
            failAnalysis(error, runGeneration)
        }
      } else if (message.type === 'frame') {
        const pending = runPending.get(message.id)
        if (pending) {
          pending.resolve(message.landmarks)
          runPending.delete(message.id)
        }
      }
    })
    worker.addEventListener(
      'error',
      (event) => {
        if (runGeneration !== runGenerationRef.current || workerRef.current !== worker) return
        const error = new Error(event.message || 'Analysis worker stopped unexpectedly.')
        readyReject(error)
        for (const pending of runPending.values()) pending.reject(error)
        runPending.clear()
        failAnalysis(error, runGeneration)
      },
      { once: true },
    )
    worker.postMessage({ type: 'init' })
    try {
      await Promise.all([
        withTimeout(
          waitUntilReady,
          30_000,
          'Pose model initialization timed out.',
          abortController.signal,
        ),
        waitForMetadata(decoderRef.current!, abortController.signal),
      ])
      if (!isCurrentRun()) return
      setStatus('analyzing')
      const decoder = decoderRef.current!
      const interval = 1 / MAX_SAMPLES_PER_SECOND
      const total = Math.floor(duration * MAX_SAMPLES_PER_SECOND) + 1
      let lastTimestamp = -1
      for (let index = 0; index < total; index += 1) {
        if (!isCurrentRun()) return
        const time = Math.min(index * interval, duration - 0.001)
        await waitForSeek(decoder, time, abortController.signal)
        if (!isCurrentRun()) return
        const dimensions = resizeDimensions(decoder.videoWidth, decoder.videoHeight)
        const bitmap = await createImageBitmap(decoder, {
          resizeWidth: dimensions.width,
          resizeHeight: dimensions.height,
        })
        if (!isCurrentRun()) {
          bitmap.close()
          return
        }
        const id = ++requestIdRef.current
        const timestamp = Math.max(time * 1000, lastTimestamp + 0.001)
        const result = new Promise<Landmark[]>((resolve, reject) =>
          runPending.set(id, { resolve, reject }),
        )
        worker.postMessage({ type: 'frame', id, bitmap, timestamp }, [bitmap])
        const landmarks = await withTimeout(
          result,
          20_000,
          'Pose inference timed out on this frame.',
          abortController.signal,
        )
        if (!isCurrentRun()) return
        lastTimestamp = timestamp
        const sample = { time, landmarks, aliased: false }
        samplesRef.current.push(sample)
        if ((index + 1) % 10 === 0 || index + 1 === total) {
          setSamples([...samplesRef.current])
          setProgress(Math.min(100, Math.round(((index + 1) / total) * 100)))
        }
      }
      if (isCurrentRun()) {
        await new Promise<void>((resolve) => {
          const timeout = window.setTimeout(() => {
            worker.terminate()
            resolve()
          }, 1000)
          worker.addEventListener('message', (event: MessageEvent<WorkerMessage>) => {
            if (event.data.type === 'disposed') {
              window.clearTimeout(timeout)
              worker.terminate()
              resolve()
            }
          })
          worker.postMessage({ type: 'dispose' })
        })
        if (!isCurrentRun()) return
        if (workerRef.current === worker) workerRef.current = null
        setSamples([...samplesRef.current])
        setProgress(100)
        setStatus('ready')
      }
    } catch (cause) {
      failAnalysis(cause, runGeneration)
    }
  }

  const seekPlayback = async (time: number) => {
    const video = videoRef.current
    if (!video) return
    playerSeekAbortRef.current?.abort()
    const controller = new AbortController()
    playerSeekAbortRef.current = controller
    const generation = ++playerSeekGenerationRef.current
    setSeekError(false)
    try {
      await waitForSeek(video, time, controller.signal)
      if (generation === playerSeekGenerationRef.current && videoRef.current === video) {
        setVideoTime(video.currentTime)
      }
    } catch {
      if (generation === playerSeekGenerationRef.current && videoRef.current === video)
        setSeekError(true)
    }
  }

  const onWorkerRetry = () => {
    void startAnalysis()
  }
  const onPlayerTime = (video: HTMLVideoElement) => setVideoTime(video.currentTime)
  const syncPausedPlayerTime = (video: HTMLVideoElement) => {
    playerSeekAbortRef.current?.abort()
    playerSeekGenerationRef.current += 1
    setIsPlaying(false)
    setVideoTime(video.currentTime)
  }
  const selectedSample = useMemo(() => {
    let closest: Sample | undefined
    for (const sample of samples) {
      if (sample.time > videoTime + 0.025) break
      closest = sample
    }
    if (!closest || videoTime - closest.time > 0.075 || !isReliable(closest.landmarks, side))
      return undefined
    return closest
  }, [samples, side, videoTime])
  const angles = selectedSample
    ? calculateAngles(selectedSample.landmarks, videoSize.width, videoSize.height, side)
    : null
  const cycles = countPedalCycles(samples, side)
  const reliableCount = samples.filter((sample) => isReliable(sample.landmarks, side)).length
  const contain = containRect(hostSize.width, hostSize.height, videoSize.width, videoSize.height)

  if (!enabled)
    return (
      <main className="bike-fitting-disabled">
        <a href="/" className="wordmark">
          <span className="wordmark-mark">↗</span>BikeMapy
        </a>
        <h1>{messages('Nastavení posedu', 'Bike fitting', language)}</h1>
        <p>
          {messages(
            'Funkce není v této verzi zapnutá.',
            'This feature is disabled in this build.',
            language,
          )}
        </p>
        <a href="/">{messages('Zpět na mapu', 'Back to routes', language)}</a>
      </main>
    )

  const chooseSide = (newSide: BikeSide) => setSide(newSide)
  const uploadLabel = messages('Vybrat video', 'Choose video', language)

  return (
    <main className="bike-fitting-app">
      <header className="bike-fitting-header">
        <a
          className="wordmark"
          href="/"
          aria-label={messages('Domů BikeMapy', 'BikeMapy home', language)}
        >
          <span className="wordmark-mark" aria-hidden="true">
            ↗
          </span>
          <span>BikeMapy</span>
        </a>
        <div className="bike-fitting-title">
          <h1>{messages('Nastavení posedu', 'Bike fitting', language)}</h1>
          <p>
            {messages(
              'Lokální analýza bočního záznamu',
              'Local side-view video analysis',
              language,
            )}
          </p>
        </div>
        <div className="bike-fitting-header-actions">
          <button
            type="button"
            className="bike-fitting-language"
            onClick={() => setLanguage((current) => (current === 'cs' ? 'en' : 'cs'))}
            aria-label={messages(
              'Přepnout jazyk na angličtinu',
              'Switch language to Czech',
              language,
            )}
          >
            {language === 'cs' ? 'CS / EN' : 'EN / CS'}
          </button>
          <a className="bike-fitting-back" href="/">
            {messages('Zpět na mapu', 'Back to routes', language)}
          </a>
        </div>
      </header>
      <div className="bike-fitting-layout">
        <section className="bike-fitting-instructions" aria-labelledby="fitting-instructions-title">
          <h2 id="fitting-instructions-title">
            {messages('Připravte záznam', 'Set up your video', language)}
          </h2>
          <p>
            {messages(
              'Postavte kolo na trenažér. Kamera má být z boku, celá postava v záběru a ruce na pákách.',
              'Use a stationary road or gravel bike on a trainer. Film from the side with your full body visible and hands on the hoods.',
              language,
            )}
          </p>
          <p>
            {messages(
              'Zvolte stranu, která je na videu dobře vidět.',
              'Choose the side that is clearly visible in the video.',
              language,
            )}
          </p>
          <label className="bike-fitting-file">
            <span>{uploadLabel}</span>
            <input
              type="file"
              accept="video/*"
              onChange={(event) => {
                const candidate = event.target.files?.[0]
                event.target.value = ''
                onFileChange(candidate)
              }}
            />
          </label>
          <p className="bike-fitting-limit">
            {messages(
              `Doporučeno ${RECOMMENDED_MIN_SECONDS}–${RECOMMENDED_MAX_SECONDS} s · nejvýše ${MAX_VIDEO_SECONDS} s · ${MAX_VIDEO_BYTES / 1024 / 1024} MB`,
              `Recommended ${RECOMMENDED_MIN_SECONDS}–${RECOMMENDED_MAX_SECONDS} s · maximum ${MAX_VIDEO_SECONDS} s · ${MAX_VIDEO_BYTES / 1024 / 1024} MB`,
              language,
            )}
          </p>
          <div
            className="bike-fitting-side"
            role="group"
            aria-label={messages('Viditelná strana', 'Visible side', language)}
          >
            <span>{messages('Viditelná strana', 'Visible side', language)}</span>
            <button type="button" aria-pressed={side === 'left'} onClick={() => chooseSide('left')}>
              {messages('Levá', 'Left', language)}
            </button>
            <button
              type="button"
              aria-pressed={side === 'right'}
              onClick={() => chooseSide('right')}
            >
              {messages('Pravá', 'Right', language)}
            </button>
          </div>
          {file && (
            <p className="bike-fitting-file-name">
              {file.name} ·{' '}
              {duration === null
                ? messages('Čtu video…', 'Reading video…', language)
                : `${duration.toFixed(1)} s`}
            </p>
          )}
          {file &&
            duration !== null &&
            (duration < RECOMMENDED_MIN_SECONDS || duration > RECOMMENDED_MAX_SECONDS) && (
              <p className="bike-fitting-note">
                {messages(
                  'Kratší nebo delší záznam může ztížit sledování pohybu.',
                  'A shorter or longer clip may make tracking less useful.',
                  language,
                )}
              </p>
            )}
          <button
            className="bike-fitting-start"
            type="button"
            onClick={() => void startAnalysis()}
            disabled={!file || duration === null || status === 'loading' || status === 'analyzing'}
          >
            {status === 'loading'
              ? messages('Načítám model…', 'Loading model…', language)
              : status === 'analyzing'
                ? messages(`Analýza ${progress} %`, `Analysing ${progress}%`, language)
                : messages('Analyzovat video', 'Analyse video', language)}
          </button>
          {status === 'loading' || status === 'analyzing' ? (
            <button
              className="bike-fitting-cancel"
              type="button"
              onClick={() => {
                stopAnalysis()
                setStatus('idle')
              }}
            >
              {messages('Zrušit analýzu', 'Cancel analysis', language)}
            </button>
          ) : null}
          {file && (
            <button className="bike-fitting-cancel" type="button" onClick={clearFile}>
              {messages('Odebrat video', 'Remove video', language)}
            </button>
          )}
          <p className="bike-fitting-privacy">
            {messages(
              'Video zůstává v tomto zařízení. Analýza běží lokálně a nepoužívá serverové API.',
              'Your video stays on this device. Analysis runs locally and does not use a server API.',
              language,
            )}
          </p>
        </section>
        <section
          className="bike-fitting-workspace"
          aria-label={messages('Přehrávač a výsledky', 'Video and measurements', language)}
        >
          <div className="bike-fitting-stage" ref={frameHostRef}>
            {videoUrl && (
              <video
                ref={videoRef}
                src={videoUrl}
                controls
                playsInline
                preload="metadata"
                onLoadedMetadata={onMetadata}
                onDurationChange={onMetadata}
                onTimeUpdate={(event) => onPlayerTime(event.currentTarget)}
                onPlay={() => setIsPlaying(true)}
                onPause={(event) => syncPausedPlayerTime(event.currentTarget)}
                onSeeked={(event) => onPlayerTime(event.currentTarget)}
                onError={() => {
                  setError(fileErrors.decode)
                  setStatus('failed')
                  stopAnalysis()
                }}
              />
            )}
            {file && videoUrl && (
              <video
                ref={decoderRef}
                className="bike-fitting-decoder"
                src={videoUrl}
                muted
                playsInline
                preload="auto"
                aria-hidden="true"
                tabIndex={-1}
              />
            )}
            {contain && selectedSample && (
              <svg
                className="bike-fitting-skeleton"
                viewBox={`0 0 ${videoSize.width} ${videoSize.height}`}
                preserveAspectRatio="xMidYMid meet"
                aria-label={messages('Kostra sledované strany', 'Tracked side skeleton', language)}
              >
                {(() => {
                  const ids =
                    side === 'left'
                      ? [11, 13, 15, 23, 25, 27, 29, 31]
                      : [12, 14, 16, 24, 26, 28, 30, 32]
                  const landmarks = selectedSample.landmarks
                  const paths = [
                    [ids[0]!, ids[1]!],
                    [ids[1]!, ids[2]!],
                    [ids[0]!, ids[3]!],
                    [ids[3]!, ids[4]!],
                    [ids[4]!, ids[5]!],
                    [ids[5]!, ids[6]!],
                    [ids[6]!, ids[7]!],
                  ]
                  const visibleIds = ids
                    .slice(0, 6)
                    .concat(ids.slice(6).filter((id) => isLandmarkReliable(landmarks[id])))
                  return (
                    <g>
                      {paths
                        .filter(
                          ([a, b]) =>
                            isLandmarkReliable(landmarks[a]) && isLandmarkReliable(landmarks[b]),
                        )
                        .map(([a, b], index) => (
                          <line
                            key={index}
                            x1={landmarks[a]!.x * videoSize.width}
                            y1={landmarks[a]!.y * videoSize.height}
                            x2={landmarks[b]!.x * videoSize.width}
                            y2={landmarks[b]!.y * videoSize.height}
                          />
                        ))}
                      {visibleIds.map((id) => (
                        <circle
                          key={id}
                          cx={landmarks[id]!.x * videoSize.width}
                          cy={landmarks[id]!.y * videoSize.height}
                          r="7"
                        />
                      ))}
                    </g>
                  )
                })()}
              </svg>
            )}
            {!file && (
              <div className="bike-fitting-empty">
                <span className="bike-fitting-frame-mark" aria-hidden="true">
                  ◉
                </span>
                <strong>
                  {messages(
                    'Vyberte boční záznam jízdy',
                    'Choose a side-view cycling video',
                    language,
                  )}
                </strong>
              </div>
            )}
            {file && !selectedSample && status === 'ready' && (
              <div className="bike-fitting-empty">
                <strong>
                  {messages(
                    'Sledování v tomto snímku není spolehlivé',
                    'Tracking is unreliable for this frame',
                    language,
                  )}
                </strong>
              </div>
            )}
          </div>
          {file && duration !== null && (
            <div className="bike-fitting-timeline">
              <label htmlFor="fitting-seek">
                {messages('Pozice videa', 'Video position', language)}
              </label>
              <input
                id="fitting-seek"
                type="range"
                min="0"
                max={duration}
                step="0.01"
                value={Math.min(videoTime, duration)}
                onChange={(event) => void seekPlayback(Number(event.target.value))}
              />
              {seekError && (
                <p role="alert">
                  {messages(
                    'Tento snímek nelze načíst.',
                    'This frame could not be decoded.',
                    language,
                  )}
                </p>
              )}
            </div>
          )}
          <div className="bike-fitting-measurements">
            <div className="bike-fitting-measurement-heading" data-analysis-status={status}>
              <h2>{messages('Úhly v tomto snímku', 'Angles in this frame', language)}</h2>
              <span>
                {status === 'analyzing'
                  ? `${progress}%`
                  : status === 'ready'
                    ? messages('HOTOVO', 'READY', language)
                    : messages('PŘIPRAVENO', 'READY', language)}
              </span>
            </div>
            {status === 'analyzing' && (
              <progress max="100" value={progress}>
                {progress}%
              </progress>
            )}
            <div className="bike-fitting-angle-grid">
              {(
                [
                  [messages('Koleno', 'Knee', language), angles?.knee],
                  [messages('Kotník', 'Ankle', language), angles?.ankle],
                  [messages('Kyčel', 'Hip', language), angles?.hip],
                  [messages('Loket', 'Elbow', language), angles?.elbow],
                  [messages('Náklon trupu', 'Torso inclination', language), angles?.torso],
                ] as const
              ).map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>
                    {value === null || value === undefined ? '—' : `${value.toFixed(0)}°`}
                  </strong>
                </div>
              ))}
            </div>
            <p>
              {reliableCount} {messages('spolehlivých snímků', 'reliable frames', language)} ·{' '}
              {cycles} {messages('dokončených otáček', 'complete pedal cycles', language)}
            </p>
            {status === 'ready' && cycles < 5 && (
              <p className="bike-fitting-warning">
                {messages(
                  'Méně než pět celých otáček; záznam nemusí stačit.',
                  'Fewer than five complete pedal cycles; the clip may be too short.',
                  language,
                )}
              </p>
            )}
            {status === 'ready' && reliableCount < 3 && (
              <p className="bike-fitting-warning">
                {messages(
                  'Sledování postavy je nespolehlivé. Zkontrolujte osvětlení, záběr a kontrast oblečení.',
                  'Pose tracking is unreliable. Check lighting, framing, and clothing contrast.',
                  language,
                )}
              </p>
            )}
            {error && (
              <div className="bike-fitting-error" role="alert">
                <strong>{error[language === 'cs' ? 0 : 1]}</strong>
                <button type="button" onClick={onWorkerRetry}>
                  {messages('Zkusit znovu', 'Try again', language)}
                </button>
              </div>
            )}
          </div>
        </section>
      </div>
      <footer className="bike-fitting-footer">
        {messages(
          'Pomůcka pro pozorování pohybu · bez doporučení nastavení',
          'Movement observation aid · no fit recommendations',
          language,
        )}
      </footer>
    </main>
  )
}
