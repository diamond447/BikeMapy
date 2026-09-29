import { expect, test, type Page } from '@playwright/test'

const backendOrigin = process.env.VITE_API_URL ?? 'http://localhost:8000'

const player = {
  player: {
    display_name: 'Rider',
    nickname: 'Rider',
    profile_image_url: null,
  },
}

async function installAccountFixtures(page: Page) {
  let authenticated = false
  let progressReads = 0

  await page.route('**/styles/liberty*', (route) =>
    route.fulfill({
      json: {
        version: 8,
        sources: {},
        layers: [{ id: 'background', type: 'background', paint: {} }],
      },
    }),
  )
  await page.route('**/api/v1/routes/**', (route) =>
    route.fulfill({ json: { count: 0, next: null, previous: null, results: [] } }),
  )
  await page.route('**/api/v1/game/auth/session/', (route) =>
    route.fulfill({ status: authenticated ? 200 : 401, json: authenticated ? player : {} }),
  )
  await page.route('**/api/v1/game/auth/local/onboard/', (route) =>
    route.fulfill({ status: 201, json: { player_id: 'player-1' } }),
  )
  await page.route('**/api/v1/game/auth/local/login/', (route) =>
    route.fulfill({ status: 200, json: { player_id: 'player-1', must_change_password: true } }),
  )
  await page.route('**/api/v1/game/account/password/', async (route) => {
    authenticated = true
    await route.fulfill({ status: 200, json: { must_change_password: false } })
  })
  await page.route(
    (url) => new URL(url).pathname === '/api/v1/game/auth/local/reset/',
    (route) =>
      route.fulfill({
        status: 202,
        json: { detail: 'If the account exists, reset instructions were sent.' },
      }),
  )
  await page.route(
    (url) => /^\/api\/v1\/game\/auth\/local\/reset\/[^/]+\/[^/]+\/$/.test(new URL(url).pathname),
    (route) => route.fulfill({ status: 200, json: { detail: 'Password reset.' } }),
  )
  await page.route('**/api/v1/game/auth/github/onboard/', (route) =>
    route.fulfill({
      status: 200,
      json: { url: `${backendOrigin}/accounts/github/login/` },
    }),
  )
  await page.route('**/api/v1/game/account/github/link/', (route) =>
    route.fulfill({
      status: 200,
      json: { url: `${backendOrigin}/accounts/github/login/?process=connect` },
    }),
  )
  await page.route('**/api/v1/game/account/uploads/', (route) =>
    route.fulfill({ status: 202, json: { batch_id: 'batch-1', status: 'queued' } }),
  )
  await page.route('**/api/v1/game/account/uploads/batch-1/', async (route) => {
    progressReads += 1
    await route.fulfill({
      status: 200,
      json: {
        batch_id: 'batch-1',
        status: 'partial',
        total_files: 2,
        processed_files: 2,
        accepted_files: 1,
        duplicate_files: 0,
        failed_files: 1,
        files: [
          { name: 'ride.gpx', status: 'accepted', activity_id: 'activity-1' },
          { name: 'broken.fit', status: 'failed', error_detail: 'Invalid FIT checksum.' },
        ],
      },
    })
  })
  await page.route('**/api/v1/game/account/activities/activity-1/', (route) =>
    route.fulfill({ status: 204, body: '' }),
  )
  await page.route('**/accounts/github/login/**', (route) =>
    route.fulfill({ status: 200, body: 'callback' }),
  )

  return {
    getProgressReads: () => progressReads,
  }
}

test('desktop completes invite, forced password, mixed upload, privacy delete, and GitHub intent', async ({
  page,
}) => {
  const fixtures = await installAccountFixtures(page)
  await page.goto('/game')
  await page.getByRole('button', { name: 'Player account' }).click()

  await page.getByRole('button', { name: 'Create invite account' }).click()
  await page.getByLabel('Username').fill('rider')
  await page.getByLabel('Email').fill('rider@example.com')
  await page.getByLabel('Competition invite code').fill('RIDE-123')
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('status')).toContainText('Check your email')

  await page.getByLabel('Username or email').fill('rider')
  await page.getByLabel('Password').fill('temporary-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByLabel('New password')).toBeVisible()
  await page.getByLabel('New password').fill('permanent-password-123')
  await page.getByRole('button', { name: 'Change password' }).click()
  await expect(page.getByRole('heading', { name: 'Import activities' })).toBeVisible()

  await page.getByRole('button', { name: 'Link GitHub' }).click()
  await expect(page).toHaveURL(`${backendOrigin}/accounts/github/login/?process=connect`)
  await page.goto('/game')
  await page.getByRole('button', { name: 'Player account' }).click()
  await expect(page.getByRole('heading', { name: 'Import activities' })).toBeVisible()
  await page.getByLabel('Activity files').setInputFiles([
    { name: 'ride.gpx', mimeType: 'application/gpx+xml', buffer: Buffer.from('<gpx />') },
    { name: 'broken.fit', mimeType: 'application/octet-stream', buffer: Buffer.from('bad') },
  ])
  await page.getByRole('checkbox', { name: /authorized/i }).check()
  await page.getByRole('button', { name: 'Upload activities' }).click()
  await expect(page.getByText('partial: 2/2 files processed')).toBeVisible()
  await expect(page.getByText('broken.fit: failed')).toBeVisible()
  await expect(page.getByText('Invalid FIT checksum.')).toBeVisible()
  expect(fixtures.getProgressReads()).toBe(1)
  await page.getByRole('button', { name: 'Delete', exact: true }).click()
  await expect(page.getByText('ride.gpx: accepted')).not.toBeVisible()
})

test.describe('mobile account recovery', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true })

  test('supports reset request, reset confirmation, and invite-gated GitHub onboarding', async ({
    page,
  }) => {
    await installAccountFixtures(page)
    await page.goto('/game')
    await page.getByRole('button', { name: 'Player account' }).click()
    await page.getByRole('button', { name: 'Forgot password' }).click()
    await page.getByLabel('Email').fill('rider@example.com')
    await page.getByRole('button', { name: 'Send reset email' }).click()
    await expect(page.getByRole('status')).toContainText('If the account exists')

    await page.goto('/game/reset-password/uid-1/token-1/')
    await page.getByRole('button', { name: 'Player account' }).click()
    await page.getByLabel('New password').fill('new-password-123')
    await page.getByRole('button', { name: 'Confirm password reset' }).click()
    await expect(page.getByRole('status')).toContainText('Password reset')

    await page.goto('/game')
    await page.getByRole('button', { name: 'Player account' }).click()
    await page.getByRole('button', { name: 'Use GitHub with an invite' }).click()
    await expect(page.getByRole('status')).toContainText('Enter your competition invite code')
    await page.getByLabel('Competition invite code').fill('RIDE-123')
    await page.getByRole('button', { name: 'Use GitHub with an invite' }).click()
    await expect(page).toHaveURL(`${backendOrigin}/accounts/github/login/`)
  })
})
