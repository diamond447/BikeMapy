/** Cloudflare Web Analytics plus three anonymous product event counters. */

export type ProductEvent = 'route_detail_view' | 'gpx_download_click' | 'original_source_click'

const WEB_ANALYTICS_SCRIPT = 'https://static.cloudflareinsights.com/beacon.min.js'
const apiBaseUrl = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

/** Add the Cloudflare beacon only when the public site token is configured. */
export function setupCloudflareWebAnalytics(documentRef: Document = document): void {
  const token = import.meta.env.VITE_CF_WEB_ANALYTICS_TOKEN?.trim()
  if (!token || documentRef.querySelector('script[data-cf-beacon]')) return

  const script = documentRef.createElement('script')
  script.type = 'module'
  script.src = WEB_ANALYTICS_SCRIPT
  script.async = true
  script.defer = true
  script.dataset.cfBeacon = JSON.stringify({ token })
  documentRef.head.appendChild(script)
}

/** Best effort: failures are ignored so analytics never affects browsing. */
export function trackProductEvent(event: ProductEvent): void {
  try {
    if (typeof navigator.sendBeacon !== 'function') return
    navigator.sendBeacon(`${apiBaseUrl}/api/v1/analytics/events/`, new URLSearchParams({ event }))
  } catch {
    // Browsers without sendBeacon (or blocked requests) do not affect the product.
  }
}
