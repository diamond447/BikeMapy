import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  // A single CI retry keeps a transient browser or network hiccup from
  // failing the run; the report lists every retried test as flaky, so a
  // pattern stays visible instead of being retried away silently.
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]]
    : 'list',
  use: { baseURL: 'http://127.0.0.1:4173', ...devices['Desktop Chrome'] },
  webServer: {
    command: 'corepack pnpm dev --host 127.0.0.1 --port 4173',
    port: 4173,
    reuseExistingServer: !process.env.CI,
  },
})
