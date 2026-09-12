import { test, expect, type Page } from '@playwright/test'

const routeId = '77777777-7777-4777-8777-777777777777'
const pointCount = 200_000
const largeGeometry = {
  type: 'LineString',
  coordinates: Array.from({ length: pointCount }, (_, index) => [
    16 + index * 0.000001,
    49 + Math.sin(index / 100) * 0.001,
  ]),
}

const json = (body: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

async function installFixtures(page: Page) {
  await page.route('**/styles/liberty*', (route) =>
    route.fulfill(
      json({
        version: 8,
        sources: {},
        layers: [
          { id: 'background', type: 'background', paint: { 'background-color': '#ceddce' } },
        ],
      }),
    ),
  )
  await page.route('**/api/v1/routes/**', (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname.endsWith('/viewport/'))
      return route.fulfill(
        json({
          mode: 'routes',
          zoom: 8,
          cells: [],
          routes: [
            {
              id: routeId,
              slug: 'large-route',
              title: 'Large route',
              geometry: {
                type: 'LineString',
                coordinates: [
                  [16, 49],
                  [16.2, 49.1],
                ],
              },
            },
          ],
          truncated: false,
        }),
      )
    if (pathname.endsWith('/geometry/'))
      return route.fulfill(
        json({ id: routeId, slug: 'large-route', title: 'Large route', geometry: largeGeometry }),
      )
    return route.fulfill(
      json({
        count: 1,
        next: null,
        previous: null,
        results: [
          {
            id: routeId,
            slug: 'large-route',
            title: 'Large route',
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

test('frames a route at the supported 200,000-point limit', async ({ page }) => {
  test.setTimeout(30_000)
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await installFixtures(page)
  await page.goto('/')
  await page.getByRole('button', { name: /large route/i }).click()
  await expect(page.locator('.route-sheet .route-detail')).toBeVisible()
  await expect(page.locator('.map-canvas')).toHaveAttribute('data-map-fit-center')
  expect(pageErrors).toEqual([])
})
