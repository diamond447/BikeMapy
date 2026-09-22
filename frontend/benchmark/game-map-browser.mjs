import { chromium } from '@playwright/test'

const [, , gameUrl, apiUrl, sessionKey] = process.argv
if (!gameUrl || !apiUrl || !sessionKey)
  throw new Error('game URL, API URL, and session key are required')

const browser = await chromium.launch()
const context = await browser.newContext()
await context.addCookies([{ name: 'sessionid', value: sessionKey, url: apiUrl }])
const samples = []
for (let index = 0; index < 30; index += 1) {
  const page = await context.newPage()
  await page.goto(gameUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-map-source-loaded="true"]')
  await page.waitForFunction(
    () => performance.getEntriesByName('game-map-response-to-idle').length > 0,
  )
  samples.push(
    await page.evaluate(
      () => performance.getEntriesByName('game-map-response-to-idle').at(-1).duration,
    ),
  )
  await page.close()
}
await browser.close()
process.stdout.write(JSON.stringify(samples))
