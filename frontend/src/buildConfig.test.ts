import { describe, expect, it } from 'vitest'

import {
  reportsEnabled,
  validateApiOrigin,
  validateProductionBuildEnvironment,
} from './buildConfig'

describe('production build configuration', () => {
  it('accepts a local API origin for local builds', () => {
    expect(validateApiOrigin('http://localhost:8000')).toBe('http://localhost:8000')
  })

  it.each([
    undefined,
    '',
    'localhost:8000',
    'ftp://api.example.test',
    'https://api.example.test/v1',
  ])('rejects an invalid API origin: %s', (value) => {
    expect(() => validateApiOrigin(value)).toThrow('VITE_API_URL')
  })

  it('rejects enabled reports without a Turnstile site key', () => {
    expect(() =>
      validateProductionBuildEnvironment({ VITE_API_URL: 'https://api.example.test' }),
    ).toThrow('VITE_TURNSTILE_SITE_KEY')
  })

  it('accepts a valid production baseline and disabled reports', () => {
    expect(() =>
      validateProductionBuildEnvironment({
        VITE_API_URL: 'https://api.example.test',
        VITE_ENABLE_REPORTS: 'true',
        VITE_TURNSTILE_SITE_KEY: 'site-key',
      }),
    ).not.toThrow()
    expect(() =>
      validateProductionBuildEnvironment({
        VITE_API_URL: 'https://api.example.test',
        VITE_ENABLE_REPORTS: 'false',
      }),
    ).not.toThrow()
  })

  it('treats case-insensitive false as disabled', () => {
    expect(reportsEnabled('FALSE')).toBe(false)
    expect(reportsEnabled(undefined)).toBe(true)
  })
})
