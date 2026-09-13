import { test, expect, type Page } from '@playwright/test'

const routeData = {
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
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const filteredRoute = {
  ...routeData,
  id: '22222222-2222-4222-8222-222222222222',
  title: 'North ridge loop',
}

const json = (body: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

async function installFixtures(page: Page, requests: string[]) {
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
  let initialListRequests = 0
  const releaseInitialLists: Array<() => void> = []
  let initialViewportRequests = 0
  const releaseInitialViewports: Array<() => void> = []
  await page.route('**/api/v1/routes/**', async (route) => {
    const requestUrl = new URL(route.request().url())
    requests.push(requestUrl.toString())
    if (requestUrl.pathname.endsWith('/viewport/') && !requestUrl.searchParams.has('search')) {
      initialViewportRequests += 1
      if (initialViewportRequests <= 2) {
        await new Promise<void>((resolve) => releaseInitialViewports.push(resolve))
        try {
          await route.fulfill(
            json({ mode: 'routes', zoom: 8, cells: [], routes: [], truncated: false }),
          )
        } catch {
          // The viewport request is expected to be aborted after the filter changes.
        }
        return
      }
    }
    if (requestUrl.pathname.endsWith('/viewport/')) {
      await route.fulfill(
        json({
          mode: 'routes',
          zoom: 8,
          cells: [],
          routes: [
            {
              ...filteredRoute,
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
      releaseInitialViewports.splice(0).forEach((release) => release())
      return
    }

    if (!requestUrl.searchParams.has('search') && initialListRequests < 2) {
      initialListRequests += 1
      await new Promise<void>((resolve) => releaseInitialLists.push(resolve))
      try {
        await route.fulfill(json({ count: 1, next: null, previous: null, results: [routeData] }))
      } catch {
        // The catalogue request is expected to be aborted after the first filter change.
      }
      return
    }
    await route.fulfill(json({ count: 1, next: null, previous: null, results: [filteredRoute] }))
    releaseInitialLists.splice(0).forEach((release) => release())
  })
}

test('debounces catalogue typing, cancels the old list, and keeps final list and map requests', async ({
  page,
}) => {
  const requests: string[] = []
  const abortedRequests: string[] = []
  page.on('requestfailed', (request) => {
    if (
      request.url().includes('/api/v1/routes/') &&
      /abort/i.test(request.failure()?.errorText ?? '')
    )
      abortedRequests.push(request.url())
  })
  await installFixtures(page, requests)
  await page.goto('/')
  await expect(page.locator('.map-canvas')).toHaveAttribute('data-map-ready', 'true')

  const search = page.getByRole('searchbox', { name: /search routes/i })
  await search.pressSequentially('ridge', { delay: 10 })
  await expect(page.getByRole('button', { name: /north ridge loop/i })).toBeVisible()
  await page.waitForTimeout(550)

  const catalogueRequests = requests.filter((url) => url.includes('/api/v1/routes/'))
  const listRequests = catalogueRequests.filter((url) => !url.includes('/viewport/'))
  const viewportRequests = catalogueRequests.filter((url) => url.includes('/viewport/'))
  expect(listRequests).toHaveLength(3)
  expect(
    viewportRequests.filter((url) => new URL(url).searchParams.get('search') === 'ridge'),
  ).toHaveLength(1)
  expect(
    abortedRequests.some(
      (url) => url.includes('/viewport/') && !new URL(url).searchParams.has('search'),
    ),
  ).toBe(true)
  expect(listRequests.filter((url) => !new URL(url).searchParams.has('search'))).toHaveLength(2)
  expect(
    listRequests.filter((url) => new URL(url).searchParams.get('search') === 'ridge'),
  ).toHaveLength(1)
  await expect(page.locator('.map-canvas')).toHaveAttribute('data-map-route-features', '1')
  expect(
    viewportRequests.filter((url) => new URL(url).searchParams.get('search') === 'ridge'),
  ).toHaveLength(1)
  expect(page.getByRole('button', { name: /south ridge loop/i })).not.toBeVisible()
})
