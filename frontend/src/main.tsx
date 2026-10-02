import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import App from './App'
import { setupCloudflareWebAnalytics } from './analytics'
import './styles.css'

const GameApp = lazy(() => import('./GameApp'))
const BikeFittingApp = lazy(() => import('./bike-fitting/BikeFittingApp'))

const queryClient = new QueryClient()
const isGamePath =
  window.location.pathname === '/game' ||
  window.location.pathname.startsWith('/game/reset-password/')
const isBikeFittingPath = window.location.pathname === '/bike-fitting'

setupCloudflareWebAnalytics()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {isBikeFittingPath ? (
        <Suspense fallback={<div className="bike-fitting-loading">Načítání… / Loading…</div>}>
          <BikeFittingApp />
        </Suspense>
      ) : isGamePath ? (
        <Suspense fallback={null}>
          <GameApp />
        </Suspense>
      ) : (
        <App />
      )}
    </QueryClientProvider>
  </StrictMode>,
)
