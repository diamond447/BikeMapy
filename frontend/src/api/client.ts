import createClient from 'openapi-fetch'

import type { paths } from './generated/schema'

export const apiBaseUrl = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

// The generated paths include the version prefix, so the base URL is the
// origin. Callers use paths such as `/api/v1/routes/` from the OpenAPI contract.
export const apiClient = createClient<paths>({ baseUrl: apiBaseUrl })
