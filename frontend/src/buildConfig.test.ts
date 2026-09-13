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
    'https://localhost:8000',
    'https://127.0.0.1:8000',
    'https://10.0.0.1',
    'https://100.64.0.1',
    'https://169.254.1.1',
    'https://172.16.0.1',
    'https://192.0.2.1',
    'https://192.168.1.20',
    'https://198.18.0.1',
    'https://203.0.113.1',
    'https://224.0.0.1',
    'https://[::1]',
    'https://[::ffff:127.0.0.1]',
    'https://[::ffff:7f00:1]',
    'https://[::ffff:192.168.1.1]',
    'https://[::ffff:c0a8:101]',
    'https://[fe80::1]',
    'https://[fe90::1]',
    'https://[febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff]',
    'https://[fec0::1]',
    'https://[2001:db8::1]',
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

  it('accepts a public IPv4-mapped IPv6 origin', () => {
    expect(validateApiOrigin('https://[::ffff:8.8.8.8]')).toBe('https://[::ffff:808:808]')
  })

  it('treats case-insensitive false as disabled', () => {
    expect(reportsEnabled('FALSE')).toBe(false)
    expect(reportsEnabled(undefined)).toBe(true)
  })
})
