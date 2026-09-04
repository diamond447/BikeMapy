import createClient from 'openapi-fetch'

const apiBaseUrl = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

/** The typed schema client is enabled once the first public API contract lands. */
export const apiClient = createClient({ baseUrl: `${apiBaseUrl}/api/v1` })
