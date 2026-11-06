import type { Route } from './types'
import type { Copy } from '../i18n/types'
import { useReportDisabled } from './useReportDisabled'
import { useReportEnabled } from './useReportEnabled'

export type { ReportController } from './useReportEnabled'
import type { ReportController } from './useReportEnabled'

// A build-time constant, so preview bundles drop the report code.
const reportImplementation =
  import.meta.env.VITE_ENABLE_REPORTS === 'false' ? useReportDisabled : useReportEnabled

export function useReport(
  route: Route | null | undefined,
  copy: Copy,
  enabled: boolean,
): ReportController {
  return reportImplementation(route, copy, enabled)
}
