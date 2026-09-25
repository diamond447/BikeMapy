import { chromium } from '@playwright/test'

const [, , gameUrl, apiUrl, sessionKey] = process.argv
if (!gameUrl || !apiUrl || !sessionKey)
  throw new Error('game URL, API URL, and session key are required')

const browser = await chromium.launch()
const context = await browser.newContext()
await context.addCookies([{ name: 'sessionid', value: sessionKey, url: apiUrl }])
const samples = []
const page = await context.newPage()
await page.goto(gameUrl, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-map-response-loaded="true"]', { state: 'attached' })
await page.waitForFunction(
  () => performance.getEntriesByName('game-map-response-to-render').length > 0,
)
const memberToggle = page.getByRole('checkbox').last()
await memberToggle.waitFor()
for (let index = 0; index < 30; index += 1) {
  const previousSamples = await page.evaluate(
    () => performance.getEntriesByName('game-map-response-to-render').length,
  )
  await memberToggle.click()
  await page.waitForFunction(
    (count) => performance.getEntriesByName('game-map-response-to-render').length > count,
    previousSamples,
  )
  samples.push(
    await page.evaluate(
      () => performance.getEntriesByName('game-map-response-to-render').at(-1).duration,
    ),
  )
}
await page.close()
await browser.close()
process.stdout.write(JSON.stringify(samples))
