import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'
import { loadEnv, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import {
  normalizePublicSiteUrl,
  renderRobots,
  renderSitemap,
  socialImageUrl,
} from './src/siteMetadata'
import { validateProductionBuildEnvironment } from './src/buildConfig'

function siteMetadataPlugin(siteUrl: string): Plugin {
  return {
    name: 'bikemapy-site-metadata',
    transformIndexHtml(html) {
      const homepage = `${siteUrl}/`
      return html.replace(
        '</head>',
        `    <meta property="og:url" content="${homepage}" />\n    <meta property="og:image" content="${socialImageUrl(siteUrl)}" />\n    <meta property="og:image:alt" content="BikeMapy cycling routes" />\n    <meta property="og:image:type" content="image/png" />\n    <meta property="og:image:width" content="1200" />\n    <meta property="og:image:height" content="630" />\n    <meta name="twitter:card" content="summary_large_image" />\n    <meta name="twitter:image" content="${socialImageUrl(siteUrl)}" />\n    <link rel="canonical" href="${homepage}" />\n  </head>`,
      )
    },
    writeBundle(options) {
      const outputDirectory = options.dir ?? resolve(process.cwd(), 'dist')
      mkdirSync(outputDirectory, { recursive: true })
      writeFileSync(resolve(outputDirectory, 'robots.txt'), renderRobots(siteUrl))
      writeFileSync(resolve(outputDirectory, 'sitemap.xml'), renderSitemap(siteUrl))
    },
  }
}

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  if (command === 'build') {
    if (!env.VITE_PUBLIC_SITE_URL?.trim()) {
      throw new Error('VITE_PUBLIC_SITE_URL is required for production builds')
    }
    validateProductionBuildEnvironment(env)
  }
  const siteUrl = normalizePublicSiteUrl(env.VITE_PUBLIC_SITE_URL)
  return {
    plugins: [react(), siteMetadataPlugin(siteUrl)],
    server: { port: 5173 },
    test: {
      environment: 'jsdom',
      setupFiles: './src/test/setup.ts',
      include: ['src/**/*.test.{ts,tsx}'],
      coverage: {
        provider: 'v8',
        reporter: ['text', 'html'],
        include: ['src/**/*.{ts,tsx}'],
        exclude: [
          'src/main.tsx',
          'src/test/**',
          'src/api/**',
          'src/vite-env.d.ts',
          '**/*.test.{ts,tsx}',
        ],
        thresholds: { lines: 80, functions: 80, branches: 70, statements: 80 },
      },
    },
  }
})
