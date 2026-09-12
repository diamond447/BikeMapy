import { useMemo, useRef, type FormEvent } from 'react'

import type { Route } from './types'
import type { Copy } from '../i18n/types'
import type { ReportController } from './useReportEnabled'

const noop = () => {}

export function useReportDisabled(
  _route: Route | null | undefined,
  _copy: Copy,
  _enabled: boolean,
): ReportController {
  void _route
  void _copy
  void _enabled
  const messageNode = useRef<HTMLTextAreaElement>(null)
  const triggerNode = useRef<HTMLButtonElement>(null)
  const dialogNode = useRef<HTMLElement>(null)
  const submitNode = useRef<HTMLButtonElement>(null)
  const turnstileNode = useRef<HTMLDivElement>(null)

  return useMemo(
    () => ({
      open: false,
      sent: false,
      reason: '',
      message: '',
      email: '',
      token: '',
      honeypot: '',
      outcome: 'idle' as const,
      error: null,
      configured: false,
      messageNode,
      dialogNode,
      turnstileNode,
      submitNode,
      triggerNode,
      setOpen: noop,
      resetVerification: noop,
      openForm: noop,
      submit: (event: FormEvent<HTMLFormElement>) => event.preventDefault(),
      invalid: noop,
      setReason: noop,
      setMessage: noop,
      setEmail: noop,
      setToken: noop,
      setHoneypot: noop,
    }),
    [],
  )
}
