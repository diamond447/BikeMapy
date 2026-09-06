import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setupCloudflareWebAnalytics, trackProductEvent } from './analytics'

describe('privacy-minimal analytics', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    document.head.querySelector('script[data-cf-beacon]')?.remove()
  })

  it('does not load the Cloudflare beacon without an explicit token', () => {
    setupCloudflareWebAnalytics()
    expect(document.head.querySelector('script[data-cf-beacon]')).toBeNull()
  })

  it('configures only the public Cloudflare token', () => {
    vi.stubEnv('VITE_CF_WEB_ANALYTICS_TOKEN', 'public-test-token')
    setupCloudflareWebAnalytics()
    const script = document.head.querySelector<HTMLScriptElement>('script[data-cf-beacon]')
    expect(script?.type).toBe('module')
    expect(script?.src).toBe('https://static.cloudflareinsights.com/beacon.min.js')
    expect(JSON.parse(script?.dataset.cfBeacon ?? '{}')).toEqual({ token: 'public-test-token' })
  })

  it('sends only an allow-listed event name without identifiers or metadata', () => {
    const beaconSpy = vi.fn().mockReturnValue(true)
    Object.defineProperty(navigator, 'sendBeacon', {
      configurable: true,
      value: beaconSpy,
    })
    trackProductEvent('route_detail_view')
    expect(beaconSpy).toHaveBeenCalledWith(
      'http://localhost:8000/api/v1/analytics/events/',
      expect.any(URLSearchParams),
    )
    expect(beaconSpy.mock.calls[0][1]?.toString()).toBe('event=route_detail_view')
  })
})
