import { test, expect, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const route = {
  id: '11111111-1111-4111-8111-111111111111',
  slug: 'south-ridge',
  title: 'South ridge loop',
  categories: [],
  distance_m: null,
  ascent_m: null,
  descent_m: null,
  loop_status: 'unknown',
  source_status: 'unknown',
  sources: [],
  variants: [],
  geometry: null,
  reviewed: false,
  elevation_profile: [
    { distance_m: 0, elevation_m: 210 },
    { distance_m: 12000, elevation_m: 460 },
  ],
  gpx_download_url: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

async function installFixtures(page: Page) {
  await page.route('**/styles/liberty*', (request) =>
    request.fulfill({
      json: {
        version: 8,
        sources: {},
        layers: [
          { id: 'background', type: 'background', paint: { 'background-color': '#c0d5ca' } },
        ],
      },
    }),
  )
  await page.route('**/api/v1/routes/**', async (request) => {
    const url = request.request().url()
    if (url.includes('/geometry/'))
      return request.fulfill({
        json: { id: route.id, slug: route.slug, title: route.title, geometry: null },
      })
    if (url.includes('/viewport/'))
      return request.fulfill({
        json: { mode: 'heatmap', zoom: 7, cells: [], routes: [], truncated: false },
      })
    if (/\/routes\/[^/]+\/$/.test(new URL(url).pathname)) return request.fulfill({ json: route })
    return request.fulfill({ json: { count: 1, next: null, previous: null, results: [route] } })
  })
}

async function expectAccessible(page: Page) {
  const result = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag22aa'])
    .analyze()
  expect(
    result.violations,
    result.violations.map((violation) => `${violation.id}: ${violation.help}`).join('\n'),
  ).toEqual([])
}

test('home, detail, Czech UI, and reporting dialog remain accessible', async ({ page }) => {
  await installFixtures(page)
  await page.goto('/')
  await expectAccessible(page)
  const zoomIn = page.getByRole('button', { name: /zoom in/i })
  const zoomOut = page.getByRole('button', { name: /zoom out/i })
  await expect(zoomIn).toBeVisible()
  await zoomIn.focus()
  await expect(zoomIn).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(zoomOut).toBeFocused()
  await page.getByRole('button', { name: /change language/i }).click()
  await expectAccessible(page)
  const routeCard = page.getByRole('button', { name: /south ridge loop/i })
  await routeCard.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: /south ridge loop/i })).toBeVisible()
  await expect(page.locator('.elevation-profile')).toBeVisible()
  await expect(page.getByText('Uncategorised')).toHaveCount(0)
  await expect(page.getByText('Distance unknown')).toHaveCount(0)
  await expect(page.locator('.route-card-meta')).toHaveCount(0)
  await expectAccessible(page)
  await page.getByRole('button', { name: /nahlásit problém/i }).click()
  const reason = page.getByRole('combobox', { name: /důvod/i })
  const message = page.getByRole('textbox', { name: /co máme prověřit/i })
  const cancel = page.getByRole('button', { name: 'Zrušit', exact: true })
  await expect(message).toBeFocused()
  await expectAccessible(page)
  await page.keyboard.press('Shift+Tab')
  await expect(reason).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(cancel).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(reason).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: /nahlásit problém/i })).toBeFocused()
})
