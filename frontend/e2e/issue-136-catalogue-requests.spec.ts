import { test, expect, type Page, type Request } from '@playwright/test'

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

async function installFixtures(page: Page, requests: string[], abortedRequests: string[]) {
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
  type HeldRequest = {
    release: () => void
    cancellationCandidate: boolean
    canceled: boolean
  }
  let initialListRequests = 0
  const releaseInitialLists = new Map<Request, HeldRequest>()
  let latestListCandidate: HeldRequest | undefined
  let resolveInitialListHeld: (() => void) | undefined
  const initialListHeld = new Promise<void>((resolve) => {
    resolveInitialListHeld = resolve
  })
  let initialViewportRequests = 0
  const releaseInitialViewports = new Map<Request, HeldRequest>()
  let latestViewportCandidate: HeldRequest | undefined
  let resolveInitialViewportHeld: (() => void) | undefined
  const initialViewportHeld = new Promise<void>((resolve) => {
    resolveInitialViewportHeld = resolve
  })
  let observeCancellations = false
  page.on('requestfailed', (request) => {
    const requestUrl = new URL(request.url())
    // Let only the matching intercepted handler finish after the browser has cancelled it.
    // Releasing another same-type request can leak a stale response into the final list.
    const heldList = releaseInitialLists.get(request)
    releaseInitialLists.delete(request)
    const heldViewport = releaseInitialViewports.get(request)
    releaseInitialViewports.delete(request)
    if (heldList) heldList.canceled = true
    if (heldViewport) heldViewport.canceled = true
    heldList?.release()
    heldViewport?.release()
    if (!observeCancellations || !/abort/i.test(request.failure()?.errorText ?? '')) return
    if (heldList?.cancellationCandidate || heldViewport?.cancellationCandidate)
      abortedRequests.push(requestUrl.toString())
  })
  await page.route('**/api/v1/routes/**', async (route) => {
    const request = route.request()
    const requestUrl = new URL(request.url())
    requests.push(requestUrl.toString())
    if (requestUrl.pathname.endsWith('/viewport/') && !requestUrl.searchParams.has('search')) {
      initialViewportRequests += 1
      if (initialViewportRequests <= 2) {
        if (latestViewportCandidate) latestViewportCandidate.cancellationCandidate = false
        const held: HeldRequest = {
          release: () => undefined,
          cancellationCandidate: true,
          canceled: false,
        }
        latestViewportCandidate = held
        await new Promise<void>((resolve) => {
          held.release = resolve
          releaseInitialViewports.set(request, held)
          resolveInitialViewportHeld?.()
        })
        releaseInitialViewports.delete(request)
        if (held.canceled) {
          try {
            await route.abort()
          } catch {
            // The browser may have already aborted the request.
          }
          return
        }
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
      return
    }

    if (!requestUrl.searchParams.has('search') && initialListRequests < 2) {
      initialListRequests += 1
      if (latestListCandidate) latestListCandidate.cancellationCandidate = false
      const held: HeldRequest = {
        release: () => undefined,
        cancellationCandidate: true,
        canceled: false,
      }
      latestListCandidate = held
      await new Promise<void>((resolve) => {
        held.release = resolve
        releaseInitialLists.set(request, held)
        if (initialListRequests === 2) resolveInitialListHeld?.()
      })
      releaseInitialLists.delete(request)
      if (held.canceled) {
        try {
          await route.abort()
        } catch {
          // The browser may have already aborted the request.
        }
        return
      }
      try {
        await route.fulfill(json({ count: 1, next: null, previous: null, results: [routeData] }))
      } catch {
        // The catalogue request is expected to be aborted after the first filter change.
      }
      return
    }
    await route.fulfill(json({ count: 1, next: null, previous: null, results: [filteredRoute] }))
  })
  return {
    waitForPendingRequests: () => Promise.all([initialListHeld, initialViewportHeld]),
    startObservingCancellations: () => {
      // Ignore startup cancellations and observe only failures caused by the upcoming filter input.
      abortedRequests.splice(0)
      observeCancellations = true
    },
  }
}

test('debounces catalogue typing, cancels the old list, and keeps final list and map requests', async ({
  page,
}) => {
  const requests: string[] = []
  const abortedRequests: string[] = []
  const fixtures = await installFixtures(page, requests, abortedRequests)
  await page.goto('/')
  await expect(page.locator('.map-canvas')).toHaveAttribute('data-map-ready', 'true')

  const search = page.getByRole('searchbox', { name: /search routes/i })
  await fixtures.waitForPendingRequests()
  fixtures.startObservingCancellations()
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
  expect(
    abortedRequests.some(
      (url) => !url.includes('/viewport/') && !new URL(url).searchParams.has('search'),
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
  await expect(page.getByRole('button', { name: /south ridge loop/i })).not.toBeVisible({
    timeout: 15_000,
  })
})
