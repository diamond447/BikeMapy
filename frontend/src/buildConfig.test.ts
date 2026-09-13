// @vitest-environment node

import { describe, expect, it } from 'vitest'
import viteConfig from '../vite.config'

import {
  reportsEnabled,
  validateApiOrigin,
  validateProductionBuildEnvironment,
} from './buildConfig'

describe('production build configuration', () => {
  it('validates alternate Vite build modes while leaving dev serve defaults alone', () => {
    const resolveConfig = viteConfig as unknown as (configEnv: {
      command: 'build' | 'serve'
      mode: string
    }) => unknown
    const previous = {
      site: process.env.VITE_PUBLIC_SITE_URL,
      api: process.env.VITE_API_URL,
      reports: process.env.VITE_ENABLE_REPORTS,
    }
    process.env.VITE_PUBLIC_SITE_URL = 'https://www.example.test'
    process.env.VITE_API_URL = 'https://api.example.test'
    process.env.VITE_ENABLE_REPORTS = 'false'

    try {
      expect(() => resolveConfig({ command: 'build', mode: 'staging' })).not.toThrow()
      delete process.env.VITE_API_URL
      expect(() => resolveConfig({ command: 'build', mode: 'staging' })).toThrow('VITE_API_URL')
      expect(() => resolveConfig({ command: 'serve', mode: 'development' })).not.toThrow()
    } finally {
      if (previous.site === undefined) delete process.env.VITE_PUBLIC_SITE_URL
      else process.env.VITE_PUBLIC_SITE_URL = previous.site
      if (previous.api === undefined) delete process.env.VITE_API_URL
      else process.env.VITE_API_URL = previous.api
      if (previous.reports === undefined) delete process.env.VITE_ENABLE_REPORTS
      else process.env.VITE_ENABLE_REPORTS = previous.reports
    }
  })

  it.each([
    undefined,
    '',
    'localhost:8000',
    'http://api.example.test',
    'http://localhost:8000',
    'https://198.51.1.1',
    'https://192.0.0.9',
    'https://192.0.0.10',
    'https://127.0.0.1',
    'https://[::1]',
    'https://[::ffff:127.0.0.1]',
    'https://[::ffff:7f00:1]',
    'https://[100::1]',
    'https://[64:ff9b:1::1]',
    'https://service.local',
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

  it('accepts public DNS hostname origins', () => {
    expect(validateApiOrigin('https://api.example.test')).toBe('https://api.example.test')
    expect(validateApiOrigin('https://api.example.invalid:8443')).toBe(
      'https://api.example.invalid:8443',
    )
  })

  it('treats case-insensitive false as disabled', () => {
    expect(reportsEnabled('FALSE')).toBe(false)
    expect(reportsEnabled(undefined)).toBe(true)
  })
})
