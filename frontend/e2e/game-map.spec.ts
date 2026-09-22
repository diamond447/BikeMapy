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
      [14, 49],
      [14.2, 49.2],
    ],
  },
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
