import createClient from 'openapi-fetch'

import type { paths } from './generated/schema'

export const apiBaseUrl = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

// The generated paths include the version prefix, so the base URL is the
// origin. Callers use paths such as `/api/v1/routes/` from the OpenAPI contract.
export const apiClient = createClient<paths>({ baseUrl: apiBaseUrl })

let csrfHeaderToken: string | null = null

export function rememberCsrfToken(response?: Response): void {
  const token = response?.headers.get('X-CSRFToken')
  if (token) csrfHeaderToken = token
}

export function csrfToken(): string | null {
  if (csrfHeaderToken) return csrfHeaderToken
  if (typeof document === 'undefined') return null
  const cookie = document.cookie.split('; ').find((value) => value.startsWith('csrftoken='))
  return cookie ? decodeURIComponent(cookie.slice('csrftoken='.length)) : null
}

export function csrfHeaders(): Record<string, string> {
  const token = csrfToken()
  return token ? { 'X-CSRFToken': token } : {}
}
