import { test, expect, type Page } from '@playwright/test'

const routeGeometry = {
  type: 'LineString',
  coordinates: [
    [16.0, 49.0],
    [16.6, 49.2],
    [17.0, 50.0],
  ],
}

const routes = [
  {
    id: '11111111-1111-4111-8111-111111111111',
    slug: 'south-moravia-ridge',
    title: 'South Moravia ridge ride',
    categories: [{ slug: 'gravel', name: 'Gravel' }],
    distance_m: '42000.00',
    ascent_m: '630.00',
    descent_m: '620.00',
    loop_status: 'loop',
    source_status: 'verified',
    sources: [
      {
        mapy_url: 'https://mapy.com/s/south-moravia-ridge',
        title: 'South Moravia ridge ride',
        status: 'verified',
        posts: [
          {
            url: 'https://bikeforum.example/threads/south-moravia#post-1',
            thread_title: 'Weekend rides around Brno',
            thread_url: 'https://bikeforum.example/threads/south-moravia',
            author: 'Jana Novakova',
          },
        ],
      },
    ],
    variants: [],
    geometry: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: '22222222-2222-4222-8222-222222222222',
    slug: 'brno-reservoir-ridge',
    title: 'Brno reservoir ridge ride',
    categories: [{ slug: 'gravel', name: 'Gravel' }],
    distance_m: '38000.00',
    ascent_m: '510.00',
    descent_m: '500.00',
    loop_status: 'loop',
    source_status: 'community',
    sources: [
      {
        mapy_url: 'https://mapy.com/s/brno-reservoir-ridge',
        title: 'Brno reservoir ridge ride',
        status: 'community',
        posts: [
          {
            url: 'https://bikeforum.example/threads/brno-reservoir#post-4',
            thread_title: 'Gravel and forest roads near Brno',
            thread_url: 'https://bikeforum.example/threads/brno-reservoir',
            author: 'Petr Dvorak',
          },
        ],
      },
    ],
    variants: [],
    geometry: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
]

const json = (body: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

async function installEvidenceFixtures(page: Page) {
  await page.route('**/styles/liberty*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        version: 8,
        sources: {},
        layers: [
          {
            id: 'evidence-background',
            type: 'background',
            paint: { 'background-color': '#c0d5ca' },
          },
        ],
      }),
    }),
  )
  await page.route('**/api/v1/routes/**', async (route) => {
    const url = new URL(route.request().url())
    const geometryMatch = url.pathname.match(/\/routes\/([^/]+)\/geometry\/$/)
    const metadataMatch = url.pathname.match(/\/routes\/([^/]+)\/$/)
    if (url.pathname.endsWith('/viewport/')) {
      await route.fulfill(
        json({
          mode: 'routes',
          zoom: 8,
          cells: [],
          routes: routes.map(({ id, slug, title }) => ({
            id,
            slug,
            title,
            geometry: routeGeometry,
          })),
          truncated: false,
        }),
      )
    } else if (geometryMatch) {
      const routeData = routes.find((item) => item.id === geometryMatch[1]) ?? routes[0]
      await route.fulfill(
        json({
          id: routeData.id,
          slug: routeData.slug,
          title: routeData.title,
          geometry: routeGeometry,
        }),
      )
    } else if (metadataMatch) {
      await route.fulfill(json(routes.find((item) => item.id === metadataMatch[1]) ?? routes[0]))
    } else {
      await route.fulfill(
        json({ count: routes.length, next: null, previous: null, results: routes }),
      )
    }
  })
}

test('captures the desktop map-first discovery state with overlap controls', async ({ page }) => {
  await installEvidenceFixtures(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.getByRole('button', { name: /south moravia ridge ride/i })).toBeVisible()
  await expect(page.getByText(/2 routes in view/i)).toBeVisible()
  await expect(page.locator('.maplibregl-canvas')).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  // MapLibre v6 may need an extra render cycle after the source data arrives.
  await page.waitForTimeout(1000)

  const map = page.locator('.map-canvas')
  const box = await map.boundingBox()
  if (!box) throw new Error('Map canvas is not measurable')
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
  await expect(page.getByText('1 / 2')).toBeVisible()
  const initialTitle = await page.locator('.route-detail h2').textContent()
  expect(['South Moravia ridge ride', 'Brno reservoir ridge ride']).toContain(initialTitle)
  await page.getByRole('button', { name: /next overlapping route/i }).click()
  await expect(page.getByText('2 / 2')).toBeVisible()
  const nextTitle = await page.locator('.route-detail h2').textContent()
  expect(['South Moravia ridge ride', 'Brno reservoir ridge ride']).toContain(nextTitle)
  expect(nextTitle).not.toBe(initialTitle)
  await page.screenshot({
    path: 'test-results/issue-10-screenshots/issue-10-desktop.png',
    animations: 'disabled',
  })
})

test('captures unobscured mobile selection and the return-to-list flow', async ({ page }) => {
  await installEvidenceFixtures(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: /south moravia ridge ride/i }).click()
  await expect(page.getByRole('heading', { name: /south moravia ridge ride/i })).toBeVisible()
  await expect(page.locator('.route-sheet .route-detail')).toBeVisible()
  const selectedToggle = page.getByRole('button', { name: /hide routes/i })
  await expect(selectedToggle).toBeVisible()
  await expect(selectedToggle).toBeDisabled()
  const detailBox = await page.locator('.route-sheet .route-detail').boundingBox()
  expect(detailBox).not.toBeNull()
  expect(detailBox?.x).toBe(0)
  expect(detailBox?.width).toBeGreaterThan(0)
  expect((detailBox?.x ?? 0) + (detailBox?.width ?? 0)).toBeLessThanOrEqual(390)
  await page.screenshot({
    path: 'test-results/issue-10-screenshots/issue-10-mobile.png',
    animations: 'disabled',
  })

  await page.getByRole('button', { name: /back to results/i }).click()
  await expect(page.locator('.route-sheet .route-detail')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /show routes/i })).toBeEnabled()
  await page.getByRole('button', { name: /show routes/i }).click()
  await expect(page.getByRole('button', { name: /south moravia ridge ride/i })).toBeVisible()
})
