import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'

/** Configure the worker once for every catalogue map instance. */
export function setMapLibreWorker(setWorker: (url: string) => void): void {
  setWorker(workerUrl)
}
