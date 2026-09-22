import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import App from './App'
import { setupCloudflareWebAnalytics } from './analytics'
import './styles.css'

const GameApp = lazy(() => import('./GameApp'))

const queryClient = new QueryClient()

setupCloudflareWebAnalytics()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {window.location.pathname === '/game' ? (
        <Suspense fallback={null}>
          <GameApp />
        </Suspense>
      ) : (
        <App />
      )}
    </QueryClientProvider>
  </StrictMode>,
)
