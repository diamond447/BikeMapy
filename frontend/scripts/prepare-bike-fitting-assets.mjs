import { createHash } from 'node:crypto'
import { mkdir, readFile, rename, rm, stat, copyFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const OUTPUT = resolve(ROOT, 'public/bike-fitting-assets')
const VERSIONED_WASM_OUTPUT = resolve(OUTPUT, 'mediapipe-1.0.1/wasm')
const WASM_SOURCE = resolve(ROOT, 'node_modules/@mediapipe/tasks-vision/wasm')
const MODEL_URL =
  'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task'
const MODEL_PATH = resolve(OUTPUT, 'pose_landmarker_lite-v1.task')
const EXPECTED_BYTES = 5_777_746
const EXPECTED_SHA256 = '59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a'
const WASM_FILES = [
  'vision_wasm_internal.js',
  'vision_wasm_internal.wasm',
  'vision_wasm_module_internal.js',
  'vision_wasm_module_internal.wasm',
  'vision_wasm_nosimd_internal.js',
  'vision_wasm_nosimd_internal.wasm',
]

export function verifyModel(buffer) {
  return (
    buffer.byteLength === EXPECTED_BYTES &&
    createHash('sha256').update(buffer).digest('hex') === EXPECTED_SHA256
  )
}

async function downloadModel() {
  let url = MODEL_URL
  for (let redirect = 0; redirect <= 3; redirect += 1) {
    const response = await fetch(url, { signal: AbortSignal.timeout(20_000), redirect: 'manual' })
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const location = response.headers.get('location')
      if (!location || redirect === 3)
        throw new Error('The model download exceeded its redirect limit.')
      url = new URL(location, url).href
      continue
    }
    if (!response.ok || !response.body)
      throw new Error(`The model download failed (${response.status}).`)
    const chunks = []
    let size = 0
    for await (const chunk of response.body) {
      size += chunk.byteLength
      if (size > EXPECTED_BYTES) throw new Error('The model download exceeded its expected size.')
      chunks.push(Buffer.from(chunk))
    }
    const buffer = Buffer.concat(chunks)
    if (!verifyModel(buffer))
      throw new Error('The downloaded model failed its size or SHA-256 check.')
    const temporaryPath = `${MODEL_PATH}.tmp`
    await writeFile(temporaryPath, buffer)
    await rename(temporaryPath, MODEL_PATH)
    return
  }
}

async function ensureModel() {
  try {
    const existing = await readFile(MODEL_PATH)
    if (verifyModel(existing)) return
    await rm(MODEL_PATH, { force: true })
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }
  await downloadModel()
}

async function prepare() {
  await mkdir(OUTPUT, { recursive: true })
  await mkdir(VERSIONED_WASM_OUTPUT, { recursive: true })
  const packageJson = JSON.parse(
    await readFile(resolve(ROOT, 'node_modules/@mediapipe/tasks-vision/package.json'), 'utf8'),
  )
  if (packageJson.version !== '1.0.1')
    throw new Error(`Expected @mediapipe/tasks-vision 1.0.1, found ${packageJson.version}.`)
  for (const file of WASM_FILES) {
    const source = resolve(WASM_SOURCE, file)
    if ((await stat(source)).size < 100_000)
      throw new Error(`MediaPipe WASM asset is missing or truncated: ${file}`)
    await copyFile(source, resolve(VERSIONED_WASM_OUTPUT, file))
  }
  await ensureModel()
  await copyFile(resolve(ROOT, 'third-party/mediapipe-LICENSE.txt'), resolve(OUTPUT, 'LICENSE.txt'))
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  prepare().catch((error) => {
    console.error(`Bike fitting assets could not be prepared: ${error.message}`)
    process.exitCode = 1
  })
}
