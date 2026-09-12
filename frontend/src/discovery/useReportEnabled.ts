import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'

import type { Route } from './types'
import type { Copy } from '../i18n/types'

type TurnstileApi = {
  render: (
    element: HTMLElement,
    options: {
      sitekey: string
      callback: (token: string) => void
      'expired-callback'?: () => void
    },
  ) => string
  reset: (widgetId?: string) => void
}

declare global {
  interface Window {
    turnstile?: TurnstileApi
  }
}

export type ReportController = {
  open: boolean
  sent: boolean
  reason: string
  message: string
  email: string
  token: string
  honeypot: string
  outcome: 'idle' | 'sending' | 'error'
  error: string | null
  configured: boolean
  messageNode: React.RefObject<HTMLTextAreaElement | null>
  dialogNode: React.RefObject<HTMLElement | null>
  turnstileNode: React.RefObject<HTMLDivElement | null>
  submitNode: React.RefObject<HTMLButtonElement | null>
  triggerNode: React.RefObject<HTMLButtonElement | null>
  setOpen: (open: boolean) => void
  resetVerification: (destroy?: boolean) => void
  openForm: () => void
  submit: (event: FormEvent<HTMLFormElement>) => void | Promise<void>
  invalid: () => void
  setReason: (value: string) => void
  setMessage: (value: string) => void
  setEmail: (value: string) => void
  setToken: (value: string) => void
  setHoneypot: (value: string) => void
}

export function useReportEnabled(
  route: Route | null | undefined,
  copy: Copy,
  enabled: boolean,
): ReportController {
  const [open, setOpen] = useState(false)
  const [sent, setSent] = useState(false)
  const [reason, setReason] = useState('incorrect_route')
  const [message, setMessage] = useState('')
  const [email, setEmail] = useState('')
  const [token, setToken] = useState('')
  const [honeypot, setHoneypot] = useState('')
  const [outcome, setOutcome] = useState<'idle' | 'sending' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const messageNode = useRef<HTMLTextAreaElement>(null)
  const triggerNode = useRef<HTMLButtonElement>(null)
  const dialogNode = useRef<HTMLElement>(null)
  const submitNode = useRef<HTMLButtonElement>(null)
  const focusTarget = useRef<HTMLElement | null>(null)
  const turnstileNode = useRef<HTMLDivElement>(null)
  const turnstileWidget = useRef<string | undefined>(undefined)
  const configured = Boolean(import.meta.env.VITE_TURNSTILE_SITE_KEY)

  const resetVerification = useCallback((destroy = false) => {
    setToken('')
    const widgetId = turnstileWidget.current
    const turnstile = window.turnstile
    const focused = document.activeElement as HTMLElement | null
    const dialog = dialogNode.current
    const target = focused && dialog?.contains(focused) ? focused : focusTarget.current
    try {
      if (widgetId && turnstile) turnstile.reset(widgetId)
    } finally {
      if (destroy) turnstileWidget.current = undefined
      if (
        target &&
        target.isConnected &&
        dialog?.contains(target) &&
        !(target instanceof HTMLButtonElement && target.disabled)
      )
        target.focus()
    }
  }, [])

  useEffect(() => {
    if (!enabled || !open || !turnstileNode.current) return
    const siteKey = import.meta.env.VITE_TURNSTILE_SITE_KEY
    if (!siteKey) return
    let disposed = false
    const render = () => {
      if (!disposed && turnstileNode.current && window.turnstile) {
        turnstileWidget.current = window.turnstile.render(turnstileNode.current, {
          sitekey: siteKey,
          callback: setToken,
          'expired-callback': resetVerification,
        })
      }
    }
    if (window.turnstile) render()
    else {
      const script =
        document.querySelector<HTMLScriptElement>('script[data-turnstile]') ??
        document.createElement('script')
      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
      script.async = true
      script.defer = true
      script.dataset.turnstile = 'true'
      script.addEventListener('load', render)
      if (!script.parentNode) document.head.appendChild(script)
    }
    return () => {
      disposed = true
      resetVerification(true)
    }
  }, [enabled, open, resetVerification])

  useEffect(() => {
    if (outcome !== 'error') return
    const target = focusTarget.current
    if (!target || !target.isConnected || !dialogNode.current?.contains(target)) return
    target.focus()
  }, [outcome])

  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    const trigger = triggerNode.current
    messageNode.current?.focus()
    const shell = document.querySelector('.app-shell')
    const background = shell
      ? Array.from(shell.children).filter((node) => !node.classList.contains('report-backdrop'))
      : []
    background.forEach((node) => node.setAttribute('inert', ''))
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !dialogNode.current) return
      const focusable = Array.from(
        dialogNode.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), textarea, a[href], input:not([disabled]):not([tabindex="-1"]), select:not([disabled])',
        ),
      )
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', closeOnEscape)
    document.addEventListener('keydown', trapFocus)
    return () => {
      document.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('keydown', trapFocus)
      background.forEach((node) => node.removeAttribute('inert'))
      ;(previous ?? trigger)?.focus()
    }
  }, [open])

  const openForm = () => {
    setSent(false)
    setOutcome('idle')
    setError(null)
    setReason('incorrect_route')
    setMessage('')
    setEmail('')
    setToken('')
    setHoneypot('')
    setOpen(true)
  }

  const invalid = () => {
    setError(copy.reportValidation)
    setOutcome('error')
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (outcome === 'sending' || !route) return
    const active = document.activeElement as HTMLElement | null
    focusTarget.current =
      active && dialogNode.current?.contains(active) ? active : submitNode.current
    setOutcome('sending')
    setError(null)
    try {
      if (!token) {
        resetVerification()
        setError(copy.reportVerification)
        setOutcome('error')
        return
      }
      const response = await fetch(
        `${import.meta.env.VITE_API_URL ?? 'http://localhost:8000'}/api/v1/routes/${route.id}/reports/`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            reason,
            message,
            contact_email: email,
            turnstile_token: token,
            website: honeypot,
          }),
        },
      )
      if (!response.ok) {
        const payload: unknown = await response.json().catch(() => ({}))
        const hasFieldErrors =
          payload !== null &&
          typeof payload === 'object' &&
          !Array.isArray(payload) &&
          Object.keys(payload).some((key) => key !== 'detail')
        const detail =
          payload !== null &&
          typeof payload === 'object' &&
          !Array.isArray(payload) &&
          typeof (payload as { detail?: unknown }).detail === 'string'
            ? (payload as { detail: string }).detail
            : ''
        const isProtectionError =
          detail === 'Security verification failed.' || detail === 'Unable to submit this report.'
        resetVerification()
        setError(
          response.status === 409
            ? copy.reportDuplicate
            : response.status === 429
              ? copy.reportRateLimited
              : response.status === 400 && isProtectionError
                ? copy.reportVerification
                : hasFieldErrors
                  ? copy.reportValidation
                  : copy.reportFailure,
        )
        setOutcome('error')
        return
      }
      setSent(true)
      setOutcome('idle')
    } catch {
      resetVerification()
      setError(copy.reportFailure)
      setOutcome('error')
    }
  }

  return {
    open,
    sent,
    reason,
    message,
    email,
    token,
    honeypot,
    outcome,
    error,
    configured,
    messageNode,
    dialogNode,
    turnstileNode,
    submitNode,
    triggerNode,
    setOpen,
    resetVerification,
    openForm,
    submit,
    invalid,
    setReason,
    setMessage,
    setEmail,
    setToken,
    setHoneypot,
  }
}
