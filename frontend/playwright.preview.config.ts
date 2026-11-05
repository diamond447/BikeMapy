import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  // A single CI retry keeps a transient browser or network hiccup from
  // failing the run; the report lists every retried test as flaky, so a
  // pattern stays visible instead of being retried away silently.
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [['list'], ['html', { open: 'never', outputFolder: 'playwright-report-preview' }]]
    : 'list',
  testMatch: 'preview-map.spec.ts',
  use: { baseURL: 'http://127.0.0.1:4174', ...devices['Desktop Chrome'] },
  webServer: {
    command:
      'VITE_PUBLIC_SITE_URL=http://127.0.0.1:4174 VITE_API_URL=https://api.example.invalid VITE_ENABLE_REPORTS=false corepack pnpm build && VITE_PUBLIC_SITE_URL=http://127.0.0.1:4174 VITE_API_URL=https://api.example.invalid VITE_ENABLE_REPORTS=false corepack pnpm exec vite preview --host 127.0.0.1 --port 4174',
    port: 4174,
    reuseExistingServer: false,
  },
})
