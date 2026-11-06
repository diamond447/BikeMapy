import createClient from 'openapi-fetch'

import type { paths } from './generated/schema'

export const apiBaseUrl = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

// Generated paths already start with `/api/v1`.
export const apiClient = createClient<paths>({ baseUrl: apiBaseUrl })
