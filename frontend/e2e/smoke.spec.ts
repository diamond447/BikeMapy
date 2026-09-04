import { test, expect } from '@playwright/test'

test('home screen exposes the map-first shell', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /find the ride/i })).toBeVisible()
  await expect(page.getByRole('region', { name: /route map/i })).toBeVisible()
})
