import { chromium } from '@playwright/test'

const [, , gameUrl, apiUrl, sessionKey] = process.argv
if (!gameUrl || !apiUrl || !sessionKey)
  throw new Error('game URL, API URL, and session key are required')

const browser = await chromium.launch()
const context = await browser.newContext()
await context.addCookies([{ name: 'sessionid', value: sessionKey, url: apiUrl }])
const samples = []
const lazySamples = []
const page = await context.newPage()
const benchmarkUrl = new URL(gameUrl)
benchmarkUrl.searchParams.set('benchmark', 'full-update')
await page.goto(benchmarkUrl.toString(), { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-map-response-loaded="true"]', { state: 'attached' })
await page.waitForFunction(
  () => performance.getEntriesByName('game-map-update-to-render').length > 0,
)
const canvas = await page.locator('.game-map-canvas').boundingBox()
if (!canvas) throw new Error('map canvas is not visible')
const projectBenchmarkPoint = (longitude, latitude) => {
  const worldSize = 512 * 2 ** 8.5
  const center = (longitude + 180) / 360
  const latitudeRadians = (latitude * Math.PI) / 180
  const vertical =
    (1 - Math.log(Math.tan(latitudeRadians) + 1 / Math.cos(latitudeRadians)) / Math.PI) / 2
  const centerX = (14.5 + 180) / 360
  const centerLatitude = (49.5 * Math.PI) / 180
  const centerY =
    (1 - Math.log(Math.tan(centerLatitude) + 1 / Math.cos(centerLatitude)) / Math.PI) / 2
  return {
    x: canvas.x + canvas.width / 2 + (center - centerX) * worldSize,
    y: canvas.y + canvas.height / 2 + (vertical - centerY) * worldSize,
  }
}
const benchmarkPoint = projectBenchmarkPoint(14.1, 49.1)
for (let index = 0; index < 30; index += 1) {
  const previousSamples = await page.evaluate(
    () => performance.getEntriesByName('game-map-lazy-interaction-to-visible').length,
  )
  await page.mouse.click(benchmarkPoint.x, benchmarkPoint.y)
  await page.waitForFunction(
    (count) => performance.getEntriesByName('game-map-lazy-interaction-to-visible').length > count,
    previousSamples,
  )
  lazySamples.push(
    await page.evaluate(
      () => performance.getEntriesByName('game-map-lazy-interaction-to-visible').at(-1).duration,
    ),
  )
  await page.getByRole('button', { name: /close trace detail/i }).click()
}
const memberToggle = page.getByRole('checkbox').last()
await memberToggle.waitFor()
for (let index = 0; index < 30; index += 1) {
  const previousSamples = await page.evaluate(
    () => performance.getEntriesByName('game-map-update-to-render').length,
  )
  await memberToggle.click()
  await page.waitForFunction(
    (count) => performance.getEntriesByName('game-map-update-to-render').length > count,
    previousSamples,
  )
  samples.push(
    await page.evaluate(
      () => performance.getEntriesByName('game-map-update-to-render').at(-1).duration,
    ),
  )
}
await page.close()
await browser.close()
process.stdout.write(JSON.stringify({ updateSamples: samples, lazySamples }))
