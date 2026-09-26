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
    { player_id: 8, display_name: 'Rider two', nickname: null, color: '#527A66', is_owner: false },
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
const CAPTURE_SOURCE_COMPLETION_BUDGET_MS = 1000

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
  await page.route('**/api/v1/game/competitions/*/capture/**', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'fresh',
        is_final: true,
        competition_id: competition.id,
        generation: 3,
        snapshot_generation: 3,
        calculated_at: '2026-09-21T12:00:00Z',
        faces: [
          {
            id: 9,
            geometry: {
              type: 'Polygon',
              coordinates: [
                [
                  [14, 49],
                  [14.1, 49],
                  [14.1, 49.1],
                  [14, 49],
                ],
              ],
            },
            area_m2: '100.000',
            effective_date: '2026-09-21',
            shared: true,
            owners: [
              {
                player_id: 7,
                display_name: 'Rider',
                nickname: null,
                color: '#F4B942',
                shared_area_m2: '50.000',
              },
              {
                player_id: 8,
                display_name: 'Rider two',
                nickname: null,
                color: '#527A66',
                shared_area_m2: '50.000',
              },
            ],
          },
        ],
        members: [
          {
            player_id: 7,
            display_name: 'Rider',
            nickname: null,
            color: '#F4B942',
            is_owner: true,
            area_m2: '50.000',
            rank: 1,
            monthly_net_change_m2: [{ month: '2026-09', net_change_m2: '12.300' }],
          },
          {
            player_id: 8,
            display_name: 'Rider two',
            nickname: null,
            color: '#527A66',
            is_owner: false,
            area_m2: '50.000',
            rank: 2,
            monthly_net_change_m2: [{ month: '2026-09', net_change_m2: '-4.500' }],
          },
        ],
        help: {},
        limits: { max_faces: 1200, max_response_bytes: 4000000 },
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
  const member = page.getByRole('checkbox').first()
  await expect(member).toBeChecked()
  await member.uncheck()
  await page.reload()
  await expect(page.getByRole('checkbox').first()).not.toBeChecked()
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

  await page.goto('/game')
  await page.getByRole('tab', { name: 'Completion' }).click()
  await expect(page.getByRole('heading', { name: 'Ride the reference lines.' })).toBeVisible()
  const route = page.getByRole('button', { name: /1 Via Czechia north/ })
  await route.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('.completion-detail').getByText('37.0%')).toBeVisible()
  await page.getByRole('tab', { name: 'Competition' }).click()
  await expect(page.getByText('Calculation pending')).toBeVisible()
  await page.getByRole('tab', { name: 'Activity' }).click()
  await expect(page.getByRole('heading', { name: 'Ride together, privately.' })).toBeVisible()
  await expect(page.locator('[data-map-source-loaded="true"]')).toBeVisible()
  await page.getByRole('tab', { name: 'Completion' }).click()
  await expect(page.getByRole('heading', { name: 'Ride the reference lines.' })).toBeVisible()
  await expect.poll(() => completionCalls).toBeGreaterThan(1)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: 'Ride the reference lines.' })).toBeVisible()
})

test('capture mode keeps global ranks while member visibility changes', async ({ page }) => {
  await page.goto('/game')
  await page.getByRole('tab', { name: 'Capture' }).click()
  await expect(page.getByRole('heading', { name: 'See what the rides claim.' })).toBeVisible()
  await expect(page.locator('[data-capture-response-loaded="true"]')).toBeVisible()
  await expect(page.locator('[data-capture-source-data-loaded="true"]')).toBeVisible()
  await expect(page.locator('[data-capture-feature-count="1"]')).toBeVisible()
  await expect
    .poll(() =>
      page.evaluate(() => performance.getEntriesByName('capture-response-to-source').length),
    )
    .toBeGreaterThan(0)
  const captureSourceDuration = await page.evaluate(() =>
    Math.max(
      ...performance.getEntriesByName('capture-response-to-source').map((entry) => entry.duration),
    ),
  )
  expect(captureSourceDuration).toBeLessThanOrEqual(CAPTURE_SOURCE_COMPLETION_BUDGET_MS)
  await expect(page.getByText('Area leaderboard')).toBeVisible()
  await expect(page.getByText('+12.3')).toBeVisible()
  await expect(page.getByText('-4.5')).toBeVisible()
  await expect(page.locator('.capture-map-legend')).toContainText('Shared')
  const captureA11y = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
  expect(captureA11y.violations, captureA11y.violations.map((item) => item.id).join(', ')).toEqual(
    [],
  )

  const secondMember = page.getByRole('checkbox', { name: 'Rider two' })
  await secondMember.uncheck()
  await expect(secondMember).not.toBeChecked()
  const secondRow = page.locator('.capture-ranking-row').filter({ hasText: 'Rider two' })
  await expect(secondRow).toContainText('2')

  await page.getByRole('tab', { name: 'Completion' }).click()
  await expect(page.getByRole('heading', { name: 'Ride the reference lines.' })).toBeVisible()
  await page.getByRole('tab', { name: 'Activity' }).click()
  await expect(page.getByRole('heading', { name: 'Ride together, privately.' })).toBeVisible()
  await page.getByRole('tab', { name: 'Capture' }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.capture-map-stage')).toBeVisible()
  await expect(page.locator('.capture-ledger')).toBeVisible()
})

test('private game journey joins by invite, syncs activity, switches maps, and removes a member', async ({
  page,
}) => {
  let activeMembers = [...competition.members]
  const joinedMember = {
    player_id: 9,
    display_name: 'New rider',
    nickname: null,
    color: '#D34D32',
    is_owner: false,
  }
  await page.unroute('**/api/v1/game/competitions/')
  await page.route('**/api/v1/game/competitions/', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        competitions: [{ ...competition, members: activeMembers }],
        active_competition_id: competition.id,
      }),
    }),
  )
  await page.route('**/api/v1/game/auth/session/', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        player: {
          id: 'player-7',
          athlete_id: '7',
          display_name: 'Rider',
          profile_image_url: null,
          nickname: null,
          lifecycle: 'connected',
          connected_at: '2026-09-21T00:00:00Z',
        },
      }),
    }),
  )
  await page.route('**/api/v1/game/account/activities/', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        sync: {
          status: 'idle',
          mode: 'incremental',
          imported_count: 4,
          rejected_count: 0,
          processed_count: 4,
          cursor_page: 2,
          last_error: '',
          completed_at: '2026-09-21T00:00:00Z',
        },
      }),
    }),
  )
  await page.route('**/api/v1/game/account/activities/full-history/', async (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        sync: {
          status: 'queued',
          mode: 'full_history',
          imported_count: 4,
          rejected_count: 0,
          processed_count: 4,
          cursor_page: 0,
          last_error: '',
          completed_at: null,
        },
      }),
    }),
  )
  await page.route('**/api/v1/game/competitions/join/', async (route) => {
    activeMembers = [...activeMembers, joinedMember]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ competition: { ...competition, members: activeMembers } }),
    })
  })
  await page.route('**/api/v1/game/competitions/*/members/8/', async (route) => {
    activeMembers = activeMembers.filter((member) => member.player_id !== 8)
    await route.fulfill({ status: 204 })
  })

  const routeId = '33333333-3333-4333-8333-333333333333'
  const routeGeometry = {
    type: 'LineString',
    coordinates: [
      [14, 49],
      [14.2, 49.2],
    ],
  }
  await page.route('**/api/v1/game/reference-routes/**', async (route) => {
    if (new URL(route.request().url()).pathname.endsWith('/completion/')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          route_id: routeId,
          version: 1,
          title: 'Via Czechia north',
          route_number: '1',
          source_kind: 'via_czechia',
          geometry: routeGeometry,
          attribution: { attribution_text: 'Via Czechia', licence: 'ODbL' },
          player: {
            status: 'fresh',
            total_length_meters: '20000.000',
            covered_length_meters: '7400.000',
            completion_percent: '37.000',
            calculated_at: '2026-09-21T00:00:00Z',
            error: '',
            covered_geometry: routeGeometry,
            monthly: [],
          },
          competition: {
            status: 'pending',
            total_length_meters: '20000.000',
            covered_length_meters: '0.000',
            completion_percent: '0.000',
            calculated_at: null,
            error: '',
            covered_geometry: null,
            monthly: [],
          },
          stages: [],
        }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        next: null,
        previous: null,
        results: [
          {
            id: routeId,
            source_identifier: 'vc-1',
            route_number: '1',
            title: 'Via Czechia north',
            operator: 'Via Czechia',
            network: 'via-czechia',
            publication_status: 'approved',
            source_kind: 'via_czechia',
            attribution: { attribution_text: 'Via Czechia' },
            stages: [],
          },
        ],
      }),
    })
  })

  await page.goto('/game')
  await page.getByRole('button', { name: 'Player account' }).click()
  await expect(page.getByRole('heading', { name: 'Your rides, kept private' })).toBeVisible()
  await page.getByPlaceholder('Enter invite code').fill('JOIN-9')
  await page.getByRole('button', { name: 'Join competition' }).click()
  await expect(page.getByText('New rider')).toBeVisible()

  await expect(page.getByText('Up to date')).toBeVisible()
  await page.getByRole('button', { name: 'Import my full Strava history' }).click()
  await expect(page.getByText(/Full history is queued/)).toBeVisible()

  await page.getByRole('tab', { name: 'Activity' }).click()
  await expect(page.getByRole('heading', { name: 'Ride together, privately.' })).toBeVisible()
  await expect(page.locator('[data-map-response-loaded="true"]')).toBeVisible()
  await expect(page.locator('[data-map-source-data-loaded="true"]')).toBeVisible()
  await expect(page.locator('[data-map-feature-count="1"]')).toBeVisible()
  await page.getByRole('tab', { name: 'Completion' }).click()
  await expect(page.getByRole('heading', { name: 'Ride the reference lines.' })).toBeVisible()
  await page.getByRole('button', { name: /1 Via Czechia north/ }).click()
  await expect(page.locator('.completion-detail').getByText('37.0%')).toBeVisible()
  await page.getByRole('tab', { name: 'Capture' }).click()
  await expect(page.getByRole('heading', { name: 'See what the rides claim.' })).toBeVisible()
  await expect(page.locator('[data-capture-response-loaded="true"]')).toBeVisible()
  await expect(page.locator('[data-capture-source-data-loaded="true"]')).toBeVisible()
  await expect(page.locator('[data-capture-feature-count="1"]')).toBeVisible()

  const memberRow = page.locator('.game-competition-member').filter({ hasText: 'Rider two' })
  await memberRow.getByRole('button', { name: 'Remove' }).click()
  await expect(memberRow).not.toBeVisible()
})

test('game map applies the latest member filter after an in-flight response', async ({ page }) => {
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
