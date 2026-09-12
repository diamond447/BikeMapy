import type { Route } from './types'
import type { Copy } from '../i18n/types'
import { useReportDisabled } from './useReportDisabled'
import { useReportEnabled } from './useReportEnabled'

export type { ReportController } from './useReportEnabled'
import type { ReportController } from './useReportEnabled'

// Vite replaces this environment access with a build-time constant. Rollup
// can therefore remove the report implementation from preview bundles.
const reportImplementation =
  import.meta.env.VITE_ENABLE_REPORTS === 'false' ? useReportDisabled : useReportEnabled

export function useReport(
  route: Route | null | undefined,
  copy: Copy,
  enabled: boolean,
): ReportController {
  return reportImplementation(route, copy, enabled)
}
