import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'
import { loadEnv, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { normalizePublicSiteUrl, renderRobots, renderSitemap } from './src/siteMetadata'

function siteMetadataPlugin(siteUrl: string): Plugin {
  return {
    name: 'bikemapy-site-metadata',
    transformIndexHtml(html) {
      const homepage = `${siteUrl}/`
      return html.replace(
        '</head>',
        `    <meta property="og:url" content="${homepage}" />\n    <link rel="canonical" href="${homepage}" />\n  </head>`,
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

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  if (mode === 'production' && !env.VITE_PUBLIC_SITE_URL?.trim()) {
    throw new Error('VITE_PUBLIC_SITE_URL is required for production builds')
  }
  const siteUrl = normalizePublicSiteUrl(env.VITE_PUBLIC_SITE_URL)
  return {
    plugins: [react(), siteMetadataPlugin(siteUrl)],
    optimizeDeps: { exclude: ['maplibre-gl'] },
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
