import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'

/** Configure the worker once for both the public catalogue and private game map. */
export function setMapLibreWorker(setWorker: (url: string) => void): void {
  setWorker(workerUrl)
}
