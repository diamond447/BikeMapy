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
for (let index = 0; index < 30; index += 1) {
  const previousSamples = await page.evaluate(
    () => performance.getEntriesByName('game-map-lazy-interaction-to-visible').length,
  )
  await page.evaluate(() =>
    globalThis.dispatchEvent(
      new globalThis.CustomEvent('bikemapy:benchmark-map-click', {
        detail: { lng: 14.06, lat: 49.06 },
      }),
    ),
  )
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
