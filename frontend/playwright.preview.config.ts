import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'preview-map.spec.ts',
  use: { baseURL: 'http://127.0.0.1:4174', ...devices['Desktop Chrome'] },
  webServer: {
    command:
      'VITE_PUBLIC_SITE_URL=http://127.0.0.1:4174 VITE_API_URL=https://api.example.invalid VITE_ENABLE_REPORTS=false corepack pnpm build && VITE_PUBLIC_SITE_URL=http://127.0.0.1:4174 VITE_API_URL=https://api.example.invalid VITE_ENABLE_REPORTS=false corepack pnpm exec vite preview --host 127.0.0.1 --port 4174',
    port: 4174,
    reuseExistingServer: false,
  },
})
