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
  type HeldRequest = { release: () => void; cancellationCandidate: boolean }
  const releaseHeldRequest = (heldRequests: Map<string, HeldRequest[]>, url: string) => {
    const queue = heldRequests.get(url)
    const held = queue?.shift()
    if (queue?.length === 0) heldRequests.delete(url)
    held?.release()
    return held
  }
  const enqueueHeldRequest = (
    heldRequests: Map<string, HeldRequest[]>,
    url: string,
    held: HeldRequest,
  ) => {
    const queue = heldRequests.get(url) ?? []
    queue.push(held)
    heldRequests.set(url, queue)
  }
  let initialListRequests = 0
  const releaseInitialLists = new Map<string, HeldRequest[]>()
  let latestListCandidate: HeldRequest | undefined
  let resolveInitialListHeld: (() => void) | undefined
  const initialListHeld = new Promise<void>((resolve) => {
    resolveInitialListHeld = resolve
  })
  let initialViewportRequests = 0
  const releaseInitialViewports = new Map<string, HeldRequest[]>()
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
    const url = requestUrl.toString()
    const heldList = releaseHeldRequest(releaseInitialLists, url)
    const heldViewport = releaseHeldRequest(releaseInitialViewports, url)
    if (!observeCancellations || !/abort/i.test(request.failure()?.errorText ?? '')) return
    if (heldList?.cancellationCandidate || heldViewport?.cancellationCandidate)
      abortedRequests.push(url)
  })
  await page.route('**/api/v1/routes/**', async (route) => {
    const requestUrl = new URL(route.request().url())
    requests.push(requestUrl.toString())
    if (requestUrl.pathname.endsWith('/viewport/') && !requestUrl.searchParams.has('search')) {
      initialViewportRequests += 1
      if (initialViewportRequests <= 2) {
        const url = requestUrl.toString()
        if (latestViewportCandidate) latestViewportCandidate.cancellationCandidate = false
        const held: HeldRequest = { release: () => undefined, cancellationCandidate: true }
        latestViewportCandidate = held
        await new Promise<void>((resolve) => {
          held.release = resolve
          enqueueHeldRequest(releaseInitialViewports, url, held)
          resolveInitialViewportHeld?.()
        })
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
      const url = requestUrl.toString()
      if (latestListCandidate) latestListCandidate.cancellationCandidate = false
      const held: HeldRequest = { release: () => undefined, cancellationCandidate: true }
      latestListCandidate = held
      await new Promise<void>((resolve) => {
        held.release = resolve
        enqueueHeldRequest(releaseInitialLists, url, held)
        if (initialListRequests === 2) resolveInitialListHeld?.()
      })
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
  expect(page.getByRole('button', { name: /south ridge loop/i })).not.toBeVisible()
})
