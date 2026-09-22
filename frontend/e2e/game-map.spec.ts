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

test.beforeEach(async ({ page }) => {
  await page.route('**/styles/liberty*', (route) =>
    route.fulfill({
      json: {
        version: 8,
        sources: {},
        layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#E8F0EC' } }],
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
        activities: [],
        truncated: false,
        limits: { max_features: 1200, max_coordinates: 120000 },
      }),
    }),
  )
})

test('game map keeps competition controls outside the public catalogue URL', async ({ page }) => {
  await page.goto('/game')
  await expect(page.getByRole('heading', { name: 'Ride together, privately.' })).toBeVisible()
  await expect(page.getByLabel('Competition', { exact: true })).toHaveValue(competition.id)
  await expect(page).not.toHaveURL(/member|competition=/)
})

test('game map controls remain usable on a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/game')
  await expect(page.getByRole('group', { name: 'Member traces' })).toBeVisible()
  await expect(page.getByRole('checkbox')).toBeChecked()
})

test('game map has no automated accessibility violations and honors reduced motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/game')
  const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
  expect(result.violations, result.violations.map((item) => item.id).join(', ')).toEqual([])
  expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true)
})
