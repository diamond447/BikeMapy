import type { Page } from '@playwright/test'

// A 1x1 transparent PNG.
const EMPTY_TILE = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  'base64',
)

/** Serve the basemap and overlay tiles locally: tests never load the real tile servers. */
export async function mockMapTiles(page: Page) {
  await page.route('https://tile.openstreetmap.org/**', (route) =>
    route.fulfill({ status: 200, contentType: 'image/png', body: EMPTY_TILE }),
  )
  await page.route('https://tile.waymarkedtrails.org/**', (route) =>
    route.fulfill({ status: 200, contentType: 'image/png', body: EMPTY_TILE }),
  )
}
