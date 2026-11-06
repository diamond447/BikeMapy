import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import { setupCloudflareWebAnalytics } from './analytics'
import { apiBaseUrl } from './api/client'
import { MAP_PROVIDER } from './mapProvider'
import { loadMapLibre } from './mapLibreLoader'
import './styles.css'

const GameApp = lazy(() => import('./GameApp'))

const isGamePath =
  window.location.pathname === '/game' ||
  window.location.pathname.startsWith('/game/reset-password/')

setupCloudflareWebAnalytics()

/** Open the connections the first view needs while the bundle still runs. */
function preconnect(url: string) {
  try {
    const link = document.createElement('link')
    link.rel = 'preconnect'
    link.href = new URL(url, window.location.href).origin
    // The public API and the map tiles are fetched without credentials.
    link.crossOrigin = 'anonymous'
    document.head.append(link)
  } catch {
    // An unparsable URL only loses the hint.
  }
}

if (!isGamePath) {
  preconnect(apiBaseUrl)
  preconnect(MAP_PROVIDER.style)
  // Start downloading the map library now; the map effect reuses the request.
  loadMapLibre().catch(() => undefined)
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {isGamePath ? (
      <Suspense fallback={null}>
        <GameApp />
      </Suspense>
    ) : (
      <App />
    )}
  </StrictMode>,
)
