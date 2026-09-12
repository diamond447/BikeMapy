import type { Route } from '../discovery/types'
import type { ReportController } from '../discovery/useReport'
import type { Copy } from '../i18n/types'

export type ReportDialogProps = {
  copy: Copy
  route: Route
  controller: ReportController
  onCancel: () => void
}

export function ReportDialog({ copy, controller, onCancel }: ReportDialogProps) {
  const {
    dialogNode,
    messageNode,
    turnstileNode,
    submitNode,
    sent: reportSent,
    reason: reportReason,
    message: reportMessage,
    email: reportEmail,
    token: reportToken,
    honeypot: reportHoneypot,
    outcome: reportOutcome,
    error: reportError,
    configured: turnstileConfigured,
    invalid: onInvalid,
    submit: onSubmit,
    setReason: onReasonChange,
    setMessage: onMessageChange,
    setEmail: onEmailChange,
    setToken: onTokenChange,
    setHoneypot: onHoneypotChange,
  } = controller
  return (
    <div className="report-backdrop" role="presentation">
      <section
        ref={dialogNode}
        className="report-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="report-title"
      >
        <h2 id="report-title">{copy.reportTitle}</h2>
        <p className="report-description">{copy.reportDescription}</p>
        {reportSent ? (
          <p role="status">{copy.reportThanks}</p>
        ) : (
          <form onInvalid={onInvalid} onSubmit={onSubmit}>
            <label htmlFor="report-reason">{copy.reportReason}</label>
            <select
              id="report-reason"
              value={reportReason}
              onChange={(event) => onReasonChange(event.target.value)}
            >
              <option value="incorrect_route">{copy.reportReasonIncorrect}</option>
              <option value="source_attribution">{copy.reportReasonSource}</option>
              <option value="author_removal">{copy.reportReasonAuthor}</option>
              <option value="rights_holder">{copy.reportReasonRights}</option>
              <option value="other">{copy.reportReasonOther}</option>
            </select>
            <label htmlFor="report-message">{copy.reportLabel}</label>
            <textarea
              id="report-message"
              ref={messageNode}
              required
              minLength={10}
              value={reportMessage}
              onChange={(event) => onMessageChange(event.target.value)}
              placeholder={copy.reportPlaceholder}
              rows={5}
            />
            <label htmlFor="report-email">{copy.reportEmail}</label>
            <input
              id="report-email"
              type="email"
              value={reportEmail}
              onChange={(event) => onEmailChange(event.target.value)}
              aria-describedby="report-email-hint"
            />
            <small id="report-email-hint">{copy.reportEmailHint}</small>
            <div className="turnstile-field" role="group" aria-labelledby="report-security-hint">
              <div ref={turnstileNode} className="turnstile-widget" />
              <input
                type="text"
                className="sr-only"
                aria-hidden="true"
                tabIndex={-1}
                name="turnstile_token"
                value={reportToken}
                onChange={(event) => onTokenChange(event.target.value)}
              />
              <p id="report-security-hint" className="report-security-hint">
                {copy.reportSecurity}
              </p>
            </div>
            <label className="report-honeypot" aria-hidden="true">
              Website
              <input
                name="website"
                tabIndex={-1}
                autoComplete="off"
                value={reportHoneypot}
                onChange={(event) => onHoneypotChange(event.target.value)}
              />
            </label>
            {reportError && (
              <p className="report-error" role="alert">
                {reportError}
              </p>
            )}
            <div className="report-actions">
              <button
                ref={submitNode}
                type="submit"
                aria-label={
                  turnstileConfigured
                    ? copy.sendReport
                    : `${copy.sendReport} (${copy.reportUnavailable})`
                }
                disabled={reportOutcome === 'sending'}
              >
                {reportOutcome === 'sending' ? copy.sendingReport : copy.sendReport}
              </button>
              <button type="button" onClick={onCancel}>
                {copy.cancel}
              </button>
            </div>
          </form>
        )}
        {reportSent && (
          <button type="button" onClick={onCancel}>
            {copy.cancel}
          </button>
        )}
      </section>
    </div>
  )
}
