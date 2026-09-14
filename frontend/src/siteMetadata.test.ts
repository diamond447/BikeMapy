import { describe, expect, it } from 'vitest'

import {
  normalizePublicSiteUrl,
  renderRobots,
  renderSitemap,
  routeUrl,
  socialImageUrl,
} from './siteMetadata'

describe('site metadata', () => {
  it('normalizes the configured public origin and defaults locally', () => {
    expect(normalizePublicSiteUrl()).toBe('http://localhost:5173')
    expect(normalizePublicSiteUrl('https://rides.example.test/')).toBe('https://rides.example.test')
    expect(() => normalizePublicSiteUrl('ftp://rides.example.test')).toThrow(/http or https/)
    expect(() => normalizePublicSiteUrl('https://user:pass@rides.example.test')).toThrow(
      /credentials/,
    )
  })

  it('builds stable absolute route URLs', () => {
    expect(routeUrl('https://rides.example.test', 'route id', 'south ridge')).toBe(
      'https://rides.example.test/?route=route+id&slug=south+ridge',
    )
    expect(socialImageUrl('https://rides.example.test/')).toBe(
      'https://rides.example.test/og-image.png',
    )
  })

  it('renders crawler metadata with absolute locations', () => {
    expect(renderRobots('https://rides.example.test')).toContain(
      'Sitemap: https://rides.example.test/sitemap.xml',
    )
    const sitemap = renderSitemap('https://rides.example.test', [
      '/?route=one&slug=south-ridge',
      '/?route=one&slug=south-ridge',
    ])
    expect(sitemap).toContain('<loc>https://rides.example.test/</loc>')
    expect(sitemap).toContain(
      '<loc>https://rides.example.test/?route=one&amp;slug=south-ridge</loc>',
    )
    expect(sitemap.match(/<url>/g)).toHaveLength(2)
  })
})
