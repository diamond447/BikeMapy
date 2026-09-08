import { test, expect, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const route = {
  id: '11111111-1111-4111-8111-111111111111',
  slug: 'atlas-loop',
  title: 'Atlas ridge loop',
  categories: [],
  distance_m: '42000',
  ascent_m: '630',
  descent_m: '620',
  loop_status: 'loop',
  source_status: 'verified',
  sources: [],
  variants: [],
  geometry: null,
  reviewed: false,
  elevation_profile: [
    { distance_m: 0, elevation_m: 210 },
    { distance_m: 18000, elevation_m: 460 },
    { distance_m: 42000, elevation_m: 290 },
  ],
  gpx_download_url: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}
const geometry = {
  type: 'LineString',
  coordinates: [
    [16, 49],
    [16.6, 49.2],
    [17, 50],
  ],
}
const json = (body: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

async function installFixtures(page: Page) {
  await page.route('**/styles/liberty*', (request) =>
    request.fulfill(
      json({
        version: 8,
        sources: {},
        layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#ceddce' } }],
      }),
    ),
  )
  await page.route('**/api/v1/routes/**', (request) => {
    const pathname = new URL(request.request().url()).pathname
    if (pathname.endsWith('/viewport/'))
      return request.fulfill(
        json({
          mode: 'routes',
          zoom: 8,
          cells: [],
          routes: [{ ...route, geometry }],
          truncated: false,
        }),
      )
    if (pathname.endsWith('/geometry/'))
      return request.fulfill(json({ id: route.id, slug: route.slug, title: route.title, geometry }))
    if (/\/routes\/[^/]+\/$/.test(pathname)) return request.fulfill(json(route))
    return request.fulfill(json({ count: 1, next: null, previous: null, results: [route] }))
  })
}

test('desktop atlas keeps one 360px working sidebar and captures evidence', async ({ page }) => {
  await installFixtures(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  const panel = page.locator('.route-panel')
  const mapStage = page.locator('.map-stage')
  await expect(panel).toBeVisible()
  await expect
    .poll(async () => {
      const box = await panel.boundingBox()
      return box ? [Math.round(box.x), Math.round(box.width)] : null
    })
    .toEqual([0, 360])
  await expect
    .poll(async () => {
      const box = await mapStage.boundingBox()
      return box ? [Math.round(box.x), Math.round(box.width)] : null
    })
    .toEqual([360, 1080])
  await expect(new AxeBuilder({ page }).analyze()).resolves.toMatchObject({ violations: [] })
  await page.getByRole('button', { name: /atlas ridge loop/i }).click()
  await expect(page.locator('.route-sheet .route-detail')).toBeVisible()
  const fitCenter = page.locator('.map-canvas')
  await expect
    .poll(async () => {
      const value = await fitCenter.getAttribute('data-map-fit-center')
      return value ? JSON.parse(value) : null
    })
    .not.toBeNull()
  const projected = JSON.parse((await fitCenter.getAttribute('data-map-fit-center')) ?? '{}') as {
    x: number
    width: number
  }
  expect(Math.abs(projected.x - projected.width / 2)).toBeLessThan(projected.width * 0.15)
  await page.screenshot({
    path: '../docs/screenshots/issue-83-desktop.png',
    animations: 'disabled',
  })
})

test('mobile atlas keeps the map visible while the sheet has three reachable positions', async ({
  page,
}) => {
  await installFixtures(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.locator('.map-stage')).toBeVisible()
  const sheetToggle = page.getByRole('button', { name: /show routes/i })
  await expect(sheetToggle).toBeVisible()
  await expect(page.locator('[data-testid="sheet-position"]')).toHaveAttribute(
    'data-position',
    'collapsed',
  )
  await page.screenshot({ path: '../docs/screenshots/issue-83-mobile.png', animations: 'disabled' })
  await sheetToggle.click()
  await expect(page.locator('[data-testid="sheet-position"]')).toHaveAttribute(
    'data-position',
    'half',
  )
  await expect(page.getByRole('button', { name: /expand routes/i })).toBeVisible()
  await page.getByRole('button', { name: /expand routes/i }).click()
  await expect(page.locator('[data-testid="sheet-position"]')).toHaveAttribute(
    'data-position',
    'full',
  )
  await page.getByRole('button', { name: /hide routes/i }).click()
  await expect(page.locator('[data-testid="sheet-position"]')).toHaveAttribute(
    'data-position',
    'collapsed',
  )
  await sheetToggle.click()
  await page.getByRole('button', { name: /atlas ridge loop/i }).click()
  await expect(page.locator('.route-sheet .route-detail')).toBeVisible()
  await expect(page.getByRole('button', { name: /hide routes/i })).toBeVisible()
  const mobileFooter = page.locator('.route-sheet .app-footer')
  const mobileLanguage = mobileFooter.getByRole('button', { name: /change language/i })
  await expect(mobileFooter).toBeVisible()
  await expect
    .poll(async () =>
      Number.parseFloat(await mobileFooter.evaluate((node) => getComputedStyle(node).fontSize)),
    )
    .toBeGreaterThanOrEqual(12)
  await expect
    .poll(async () =>
      Number.parseFloat(await mobileLanguage.evaluate((node) => getComputedStyle(node).fontSize)),
    )
    .toBeGreaterThanOrEqual(12)
  await page.screenshot({
    path: '../docs/screenshots/issue-83-mobile-detail.png',
    animations: 'disabled',
  })
})
