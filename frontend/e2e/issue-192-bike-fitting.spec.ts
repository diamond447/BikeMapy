import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

test('loads same-origin MediaPipe assets and analyses a local synthetic video', async ({
  page,
}) => {
  const requestedAssets: string[] = []
  const completedRemotePosts: string[] = []
  const workerPolicies: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname.includes('/bike-fitting-assets/')) requestedAssets.push(url.pathname)
  })
  page.on('requestfinished', (request) => {
    const url = new URL(request.url())
    if (request.method() === 'POST' && url.origin !== 'http://127.0.0.1:4173') {
      completedRemotePosts.push(request.url())
    }
  })
  page.on('response', (response) => {
    if (new URL(response.url()).pathname.includes('pose.worker')) {
      const policy = response.headers()['content-security-policy']
      if (policy) workerPolicies.push(policy)
    }
  })
  await page.goto('/bike-fitting')
  await expect(page.getByRole('heading', { name: 'Bike fitting' })).toBeVisible()

  const bytes = await readFile(new URL('./fixtures/issue-192-blank-1s.webm', import.meta.url))
  await page.locator('input[type=file]').setInputFiles({
    name: 'synthetic-ride.webm',
    mimeType: 'video/webm',
    buffer: bytes,
  })
  await expect(page.getByRole('button', { name: 'Analyse video' })).toBeEnabled()
  await page.getByRole('button', { name: 'Analyse video' }).click()
  const analysisStatus = page.locator('.bike-fitting-measurement-heading')
  await expect(analysisStatus).toHaveAttribute('data-analysis-status', 'analyzing', {
    timeout: 60_000,
  })
  await expect(analysisStatus).toHaveAttribute('data-analysis-status', 'ready', { timeout: 60_000 })
  expect(requestedAssets).toContain('/bike-fitting-assets/pose_landmarker_lite-v1.task')
  expect(requestedAssets.some((path) => path.endsWith('vision_wasm_module_internal.js'))).toBe(true)
  expect(requestedAssets.some((path) => path.endsWith('vision_wasm_module_internal.wasm'))).toBe(
    true,
  )
  expect(completedRemotePosts).toEqual([])
  expect(workerPolicies.some((policy) => policy.includes("connect-src 'self'"))).toBe(true)
})
