import { test, expect, type Page } from '@playwright/test'

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
  elevation_profile: [],
  gpx_download_url: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
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
        layers: [
          { id: 'background', type: 'background', paint: { 'background-color': '#c0d5ca' } },
        ],
      }),
    ),
  )
  await page.route('**/api/v1/routes/**', (request) => {
    const pathname = new URL(request.request().url()).pathname
    if (pathname.endsWith('/viewport/'))
      return request.fulfill(
        json({ mode: 'heatmap', zoom: 7, cells: [], routes: [], truncated: false }),
      )
    if (pathname.endsWith('/geometry/'))
      return request.fulfill(
        json({ id: route.id, slug: route.slug, title: route.title, geometry: null }),
      )
    if (/\/routes\/[^/]+\/$/.test(pathname)) return request.fulfill(json(route))
    return request.fulfill(json({ count: 1, next: null, previous: null, results: [route] }))
  })
}

async function expectKeyboardRouteFocus(page: Page) {
  const card = page.getByRole('button', { name: /south ridge loop/i })
  await card.focus()
  await page.keyboard.press('Enter')
  const heading = page.getByRole('heading', { name: /south ridge loop/i })
  await expect(heading).toBeFocused()

  const back = page.getByRole('button', { name: /back to results/i })
  await back.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('[data-testid="sheet-position"]')).toHaveAttribute(
    'data-position',
    /half|full/,
  )
  await expect(card).toBeVisible()
  await expect(card).toBeFocused()
}

test('desktop keyboard route navigation moves into detail and restores the card', async ({
  page,
}) => {
  await installFixtures(page)
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/')
  await expectKeyboardRouteFocus(page)
})

test('keyboard close returns focus after pointer and URL route selection', async ({ page }) => {
  await installFixtures(page)
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/')

  const card = page.getByRole('button', { name: /south ridge loop/i })
  await card.click()
  await expect(page.getByRole('heading', { name: /south ridge loop/i })).toBeVisible()
  const close = page.getByRole('button', { name: /close route details/i })
  await close.focus()
  await page.keyboard.press('Enter')
  await expect(card).toBeFocused()

  await page.goto('/?route=99999999-9999-4999-8999-999999999999')
  const heading = page.getByRole('heading', { name: /south ridge loop/i })
  await expect(heading).toBeVisible()
  await expect(heading).not.toBeFocused()
  await page.getByRole('button', { name: /close route details/i }).focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('.route-list')).toBeFocused()
})

test('mobile browser Back after pointer selection does not apply keyboard restoration', async ({
  page,
}) => {
  await installFixtures(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: /show routes/i }).click()
  await page.getByRole('button', { name: /south ridge loop/i }).click()

  const mapStage = page.locator('.map-stage')
  await mapStage.evaluate((element) => {
    element.setAttribute('tabindex', '-1')
    ;(element as HTMLElement).focus()
  })
  await page.goBack()
  await expect(page.locator('.route-sheet .route-detail')).toHaveCount(0)
  await expect(page.locator('[data-testid="sheet-position"]')).not.toHaveAttribute(
    'data-position',
    'half',
  )
  await expect(mapStage).toBeFocused()
})

test('mobile keyboard route navigation moves into detail and restores the card', async ({
  page,
}) => {
  await installFixtures(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: /show routes/i }).click()
  await expectKeyboardRouteFocus(page)
})
