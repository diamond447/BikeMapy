import { test, expect, type Page } from '@playwright/test'

const routeGeometry = {
  type: 'LineString',
  coordinates: [
    [16.0, 49.0],
    [16.6, 49.2],
    [17.0, 50.0],
  ],
}

const json = (body: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

async function installPreviewFixtures(page: Page) {
  await page.route('**/styles/liberty*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        version: 8,
        sources: {},
        layers: [
          {
            id: 'preview-background',
            type: 'background',
            paint: { 'background-color': '#c0d5ca' },
          },
        ],
      }),
    }),
  )
  await page.route('**/api/v1/routes/**', (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/viewport/'))
      return route.fulfill(
        json({
          mode: 'routes',
          zoom: 8,
          cells: [],
          routes: [
            {
              id: '11111111-1111-4111-8111-111111111111',
              slug: 'preview-route',
              title: 'Preview route',
              geometry: routeGeometry,
            },
          ],
          truncated: false,
        }),
      )
    return route.fulfill(
      json({
        count: 1,
        next: null,
        previous: null,
        results: [
          {
            id: '11111111-1111-4111-8111-111111111111',
            slug: 'preview-route',
            title: 'Preview route',
            categories: [],
            distance_m: null,
            ascent_m: null,
            descent_m: null,
            loop_status: 'unknown',
            source_status: 'unknown',
            sources: [],
            variants: [],
            geometry: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ],
      }),
    )
  })
}

test('production preview renders MapLibre sources and loads its worker', async ({ page }) => {
  const workerResponses: number[] = []
  const workerFailures: string[] = []
  const mapErrors: string[] = []

  page.on('response', (response) => {
    if (/maplibre-gl-worker(?:[-./]|$)/i.test(new URL(response.url()).pathname))
      workerResponses.push(response.status())
  })
  page.on('requestfailed', (request) => {
    if (/maplibre-gl-worker(?:[-./]|$)/i.test(new URL(request.url()).pathname))
      workerFailures.push(request.url())
  })
  page.on('pageerror', (error) => {
    if (/worker|maplibre/i.test(error.message)) mapErrors.push(error.message)
  })
  page.on('console', (message) => {
    if (message.type() === 'error' && /worker|maplibre/i.test(message.text()))
      mapErrors.push(message.text())
  })

  await installPreviewFixtures(page)
  await page.goto('/')

  const map = page.locator('.map-canvas')
  await expect(map).toHaveAttribute('data-map-ready', 'true')
  await expect(map).toHaveAttribute(
    'data-map-sources',
    'browse-heatmap,browse-routes,selected-route',
  )
  await expect(map).toHaveAttribute('data-map-route-features', '1')
  await expect
    .poll(async () => Number(await map.getAttribute('data-map-rendered-features')))
    .toBeGreaterThan(0)
  await expect(map.locator('.maplibregl-canvas')).toBeVisible()
  await expect(page.getByText('1 routes in view')).toBeVisible()

  expect(workerResponses).toContain(200)
  expect(workerFailures).toEqual([])
  expect(mapErrors).toEqual([])
})
