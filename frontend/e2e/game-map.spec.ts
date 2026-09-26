import { expect, test } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const competition = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'Autumn loop',
  invite_code: 'hidden',
  owner_player_id: 7,
  is_owner: true,
  is_active: true,
  is_selected: true,
  color: '#F4B942',
  created_at: '2026-09-21T00:00:00Z',
  members: [
    { player_id: 7, display_name: 'Rider', nickname: null, color: '#F4B942', is_owner: true },
  ],
}
const activity = {
  id: '22222222-2222-4222-8222-222222222222',
  player_id: 7,
  calendar_date: '2026-09-21',
  geometry: {
    type: 'LineString',
    coordinates: [
      [16.5, 49.15],
      [16.7, 49.25],
    ],
  },
}
const largeCompetition = {
  ...competition,
  members: Array.from({ length: 101 }, (_, index) => ({
    player_id: index === 0 ? competition.members[0].player_id : index + 100,
    display_name: index === 0 ? 'Rider' : `Rider ${index + 100}`,
    nickname: null,
    color: index === 0 ? '#F4B942' : '#3A86FF',
    is_owner: index === 0,
  })),
}

test.beforeEach(async ({ page }) => {
  await page.route('**/styles/liberty*', (route) =>
    route.fulfill({
      json: {
        version: 8,
        sources: {},
        layers: [
          { id: 'background', type: 'background', paint: { 'background-color': '#E8F0EC' } },
        ],
      },
    }),
  )
  await page.route('**/api/v1/game/competitions/', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ competitions: [competition], active_competition_id: competition.id }),
    }),
  )
  await page.route('**/api/v1/game/competitions/*/map/**', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'loaded',
        competition_id: competition.id,
        members: competition.members,
        activities: [activity],
        truncated: false,
        limits: { max_features: 1200, max_coordinates: 120000 },
      }),
    }),
  )
})

test('game map keeps competition controls outside the public catalogue URL', async ({ page }) => {
  const workerFailures: string[] = []
  page.on('requestfailed', (request) => {
    if (request.url().includes('worker')) workerFailures.push(request.url())
  })
  await page.goto('/game')
  await expect(page.getByRole('heading', { name: 'Ride together, privately.' })).toBeVisible()
  await expect(page.locator('[data-map-source-loaded="true"]')).toBeVisible()
  await expect(page.locator('.maplibregl-canvas')).toBeVisible()
  await expect(page.getByLabel('Competition', { exact: true })).toHaveValue(competition.id)
  const trace = page.getByRole('button', { name: activity.calendar_date })
  await trace.focus()
  await page.keyboard.press('Enter')
  await expect(
    page.getByRole('complementary', { name: 'Trace detail' }).getByText(activity.calendar_date),
  ).toBeVisible()
  const close = page.getByRole('button', { name: /close trace detail/i })
  await expect(close).toBeFocused()
  await close.click()
  await expect(trace).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(close).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(trace).toBeFocused()
  await expect(page).not.toHaveURL(/member|competition=/)
  expect(workerFailures).toEqual([])
})

test('game map controls remain usable on a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/game')
  await expect(page.getByRole('group', { name: 'Member traces' })).toBeVisible()
  const member = page.getByRole('checkbox')
  await expect(member).toBeChecked()
  await member.uncheck()
  await page.reload()
  await expect(page.getByRole('checkbox')).not.toBeChecked()
})

test('game map has no automated accessibility violations and honors reduced motion', async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/game')
  const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
  expect(result.violations, result.violations.map((item) => item.id).join(', ')).toEqual([])
  expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(
    true,
  )
})

test('game map applies the latest member filter after an in-flight response', async ({ page }) => {
  await page.unroute('**/api/v1/game/competitions/')
  await page.route('**/api/v1/game/competitions/', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        competitions: [largeCompetition],
        active_competition_id: largeCompetition.id,
      }),
    }),
  )
  await page.unroute('**/api/v1/game/competitions/*/map/**')
  let mapCalls = 0
  await page.route('**/api/v1/game/competitions/*/map/**', async (route) => {
    mapCalls += 1
    const url = new URL(route.request().url())
    const selected = url.searchParams.getAll('member')
    if (mapCalls === 1) expect(selected.length).toBeLessThanOrEqual(100)
    if (mapCalls === 2) await page.waitForTimeout(300)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: selected.length && selected[0] !== '0' ? 'loaded' : 'empty',
        competition_id: largeCompetition.id,
        members: largeCompetition.members,
        activities:
          selected.length && selected[0] !== '0'
            ? [
                {
                  ...activity,
                  calendar_date: mapCalls >= 3 ? '2026-09-23' : activity.calendar_date,
                },
              ]
            : [],
        truncated: false,
        limits: { max_features: 1200, max_coordinates: 120000 },
      }),
    })
  })

  await page.goto('/game')
  await expect(page.locator('[data-map-response-loaded="true"]')).toBeVisible()
  const member = page.getByRole('checkbox').first()
  const toggleRequest = page.waitForRequest(
    (request) =>
      request.url().includes('/api/v1/game/competitions/') && request.url().includes('/map/'),
  )
  await member.uncheck()
  await toggleRequest
  await member.check()
  await expect(page.getByRole('button', { name: '2026-09-23' })).toBeVisible()
})

test('game map resolves a grouped line click to an authorized activity', async ({ page }) => {
  await page.goto('/game')
  await expect(page.locator('[data-map-response-loaded="true"]')).toBeVisible()
  const canvas = await page.locator('.game-map-canvas').boundingBox()
  expect(canvas).not.toBeNull()
  if (!canvas) return
  await page.mouse.click(canvas.x + canvas.width / 2, canvas.y + canvas.height / 2)
  await expect(page.getByRole('complementary', { name: 'Trace detail' })).toContainText(
    activity.calendar_date,
  )
})

test('game map Retry repeats a failed map request', async ({ page }) => {
  await page.unroute('**/api/v1/game/competitions/*/map/**')
  let mapCalls = 0
  await page.route('**/api/v1/game/competitions/*/map/**', async (route) => {
    mapCalls += 1
    if (mapCalls === 1) {
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'loaded',
        competition_id: competition.id,
        members: competition.members,
        activities: [activity],
        truncated: false,
        limits: { max_features: 1200, max_coordinates: 120000 },
      }),
    })
  })

  const failedMapRequest = page.waitForRequest(
    (request) =>
      request.url().includes('/api/v1/game/competitions/') && request.url().includes('/map/'),
  )
  await page.goto('/game')
  await failedMapRequest
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('button', { name: /retry/i }).click()
  await expect(page.getByRole('button', { name: activity.calendar_date })).toBeVisible()
  expect(mapCalls).toBe(2)
})

test('game map wraps dateline geometry and normalizes world-copy bounds', async ({ page }) => {
  await page.unroute('**/api/v1/game/competitions/*/map/**')
  await page.route('**/api/v1/game/competitions/*/map/**', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'loaded',
        competition_id: competition.id,
        members: competition.members,
        activities: [
          {
            ...activity,
            geometry: {
              type: 'LineString',
              coordinates: [
                [179.5, 49],
                [-179.5, 49.1],
              ],
            },
          },
        ],
        truncated: false,
        limits: { max_features: 1200, max_coordinates: 120000 },
      }),
    }),
  )
  await page.goto('/game?benchmark=full-update')
  await expect(page.locator('[data-map-response-loaded="true"]')).toBeVisible()
  const requestPromise = page.waitForRequest(
    (request) =>
      request.url().includes('/api/v1/game/competitions/') && request.url().includes('/map/'),
  )
  await page.evaluate(() =>
    globalThis.dispatchEvent(
      new CustomEvent('bikemapy:benchmark-map-pan', {
        detail: { center: [180, 49], zoom: 8 },
      }),
    ),
  )
  const request = await requestPromise
  const url = new URL(request.url())
  const west = Number(url.searchParams.get('west'))
  const east = Number(url.searchParams.get('east'))
  expect(west).toBeGreaterThanOrEqual(-180)
  expect(west).toBeLessThanOrEqual(180)
  expect(east).toBeGreaterThanOrEqual(-180)
  expect(east).toBeLessThanOrEqual(180)
  expect((east - west + 360) % 360).toBeLessThanOrEqual(120)
  await expect(page.locator('.game-map-render-overlay')).toBeVisible()
  expect(
    await page.locator('.game-map-render-overlay').evaluate((canvas) => {
      const context = canvas.getContext('2d')
      if (!context) return false
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data
      for (let index = 3; index < pixels.length; index += 4) {
        if (pixels[index] !== 0) return true
      }
      return false
    }),
  ).toBe(true)
})
