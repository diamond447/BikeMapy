import { useMemo, useState } from 'react'

import { apiBaseUrl, csrfHeaders, rememberCsrfToken } from '../api/client'

type UploadResult = {
  name: string
  status: string
  error_code?: string
  activity_id?: string | null
}

type Batch = {
  batch_id: string
  status: string
  total_files: number
  processed_files: number
  accepted_files: number
  duplicate_files: number
  failed_files: number
  files: UploadResult[]
}

type Props = {
  authenticated?: boolean
  onAuthenticated?: () => void
  onSignedOut?: () => void
}

const endpoint = (path: string) => `${apiBaseUrl.replace(/\/$/, '')}${path}`

async function request(path: string, init: RequestInit = {}) {
  const response = await fetch(endpoint(path), {
    credentials: 'include',
    ...init,
    headers: { ...csrfHeaders(), ...(init.headers ?? {}) },
  })
  rememberCsrfToken(response)
  let data: unknown = null
  try {
    data = await response.json()
  } catch {
    // Empty 204 responses are expected for logout.
  }
  return { response, data }
}

function detail(data: unknown, fallback: string) {
  return data && typeof data === 'object' && 'detail' in data && typeof data.detail === 'string'
    ? data.detail
    : fallback
}

export function LocalAccountPanel({ authenticated = false, onAuthenticated, onSignedOut }: Props) {
  const [mode, setMode] = useState<'login' | 'onboard' | 'reset' | 'change'>(
    authenticated ? 'change' : 'login',
  )
  const [identifier, setIdentifier] = useState('')
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [attested, setAttested] = useState(false)
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])
  const [batch, setBatch] = useState<Batch | null>(null)

  const resetMessage = () => setMessage(null)
  const canUpload = useMemo(
    () => selectedFiles.length > 0 && attested && !busy,
    [selectedFiles, attested, busy],
  )

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setBusy(true)
    resetMessage()
    try {
      if (mode === 'login') {
        const result = await request('/api/v1/game/auth/local/login/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ identifier, password }),
        })
        if (!result.response.ok) {
          setMessage(detail(result.data, 'Invalid username, email, or password.'))
        } else if (
          result.data &&
          typeof result.data === 'object' &&
          'must_change_password' in result.data &&
          result.data.must_change_password
        ) {
          setMode('change')
          setMessage('Change your temporary password before accessing the game.')
        } else {
          setMessage('Signed in.')
          onAuthenticated?.()
        }
      } else if (mode === 'onboard') {
        const result = await request('/api/v1/game/auth/local/onboard/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, email, invite_code: inviteCode }),
        })
        if (!result.response.ok) {
          setMessage(detail(result.data, 'Unable to create this account.'))
        } else {
          setMode('login')
          setMessage('Account created. Check your email for the temporary password.')
        }
      } else if (mode === 'reset') {
        await request('/api/v1/game/auth/local/reset/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email }),
        })
        setMessage('If the account exists, reset instructions were sent.')
      } else {
        const result = await request('/api/v1/game/account/password/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password }),
        })
        if (!result.response.ok) setMessage(detail(result.data, 'Password change failed.'))
        else {
          setMode('change')
          setPassword('')
          setMessage('Password changed.')
          onAuthenticated?.()
        }
      }
    } catch {
      setMessage('The account service is temporarily unavailable.')
    } finally {
      setBusy(false)
    }
  }

  const upload = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canUpload) return
    setBusy(true)
    resetMessage()
    const form = new FormData()
    form.append('attested', 'true')
    selectedFiles.forEach((file) => form.append('files', file, file.name))
    try {
      const result = await request('/api/v1/game/account/uploads/', { method: 'POST', body: form })
      if (!result.response.ok) setMessage(detail(result.data, 'Upload could not be started.'))
      else if (result.data && typeof result.data === 'object' && 'batch_id' in result.data) {
        const batchId = String(result.data.batch_id)
        setMessage('Upload queued.')
        for (let attempt = 0; attempt < 120; attempt += 1) {
          const progress = await request(`/api/v1/game/account/uploads/${batchId}/`)
          if (progress.data && typeof progress.data === 'object' && 'files' in progress.data) {
            setBatch(progress.data as Batch)
          }
          const progressStatus =
            progress.data && typeof progress.data === 'object' && 'status' in progress.data
              ? String(progress.data.status)
              : ''
          if (['completed', 'partial', 'failed'].includes(progressStatus)) break
          await new Promise((resolve) => window.setTimeout(resolve, 1000))
        }
      }
    } catch {
      setMessage('The upload service is temporarily unavailable.')
    } finally {
      setBusy(false)
    }
  }

  const beginGithubOnboarding = async () => {
    if (!inviteCode.trim()) {
      setMode('onboard')
      setMessage('Enter your competition invite code, then continue with GitHub.')
      return
    }
    setBusy(true)
    try {
      const result = await request('/api/v1/game/auth/github/onboard/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ invite_code: inviteCode }),
      })
      if (!result.response.ok) setMessage(detail(result.data, 'The invite code is not valid.'))
      else window.location.assign('/accounts/github/login/')
    } catch {
      setMessage('The account service is temporarily unavailable.')
    } finally {
      setBusy(false)
    }
  }

  const deleteActivity = async (activityId: string) => {
    const result = await request(`/api/v1/game/account/activities/${activityId}/`, {
      method: 'DELETE',
    })
    if (result.response.ok)
      setBatch(
        (current) =>
          current && {
            ...current,
            files: current.files.filter((file) => file.activity_id !== activityId),
          },
      )
    else setMessage('The activity could not be deleted.')
  }

  if (authenticated) {
    return (
      <section className="local-account-upload" aria-labelledby="local-upload-title">
        <div className="local-account-heading">
          <h3 id="local-upload-title">Import activities</h3>
          <a href="/accounts/github/login/?process=connect">Link GitHub</a>
        </div>
        <p>Upload FIT, GPX, or TCX files, optionally in a ZIP archive.</p>
        <form onSubmit={upload}>
          <label htmlFor="activity-files">Activity files</label>
          <input
            id="activity-files"
            type="file"
            multiple
            accept=".fit,.gpx,.tcx,.zip"
            onChange={(event) => setSelectedFiles(Array.from(event.target.files ?? []))}
          />
          <label className="local-account-check">
            <input
              type="checkbox"
              checked={attested}
              onChange={(event) => setAttested(event.target.checked)}
            />
            I own or am authorized to process these activities.
          </label>
          <button type="submit" disabled={!canUpload}>
            {busy ? 'Processing…' : 'Upload activities'}
          </button>
        </form>
        {message && <p role="status">{message}</p>}
        {batch && (
          <div aria-live="polite">
            <p>
              {batch.status}: {batch.processed_files}/{batch.total_files} files processed
            </p>
            <ul>
              {batch.files.map((file) => (
                <li key={`${file.name}-${file.activity_id ?? file.status}`}>
                  <span>
                    {file.name}: {file.status}
                  </span>
                  {file.activity_id && (
                    <button type="button" onClick={() => void deleteActivity(file.activity_id!)}>
                      Delete
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    )
  }

  return (
    <section className="local-account-panel" aria-labelledby="local-account-title">
      <h2 id="local-account-title">BikeMapy player account</h2>
      {message && <p role="status">{message}</p>}
      <form onSubmit={submit}>
        {mode === 'login' && (
          <>
            <label htmlFor="local-identifier">Username or email</label>
            <input
              id="local-identifier"
              required
              value={identifier}
              onChange={(event) => setIdentifier(event.target.value)}
              autoComplete="username"
            />
            <label htmlFor="local-password">Password</label>
            <input
              id="local-password"
              required
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
            />
          </>
        )}
        {mode === 'onboard' && (
          <>
            <label htmlFor="local-username">Username</label>
            <input
              id="local-username"
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
            />
            <label htmlFor="local-email">Email</label>
            <input
              id="local-email"
              required
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
            />
            <label htmlFor="local-invite">Competition invite code</label>
            <input
              id="local-invite"
              required
              value={inviteCode}
              onChange={(event) => setInviteCode(event.target.value)}
            />
          </>
        )}
        {mode === 'reset' && (
          <>
            <label htmlFor="local-reset-email">Email</label>
            <input
              id="local-reset-email"
              required
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
            />
          </>
        )}
        {mode === 'change' && (
          <>
            <label htmlFor="local-new-password">New password</label>
            <input
              id="local-new-password"
              required
              minLength={12}
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="new-password"
            />
          </>
        )}
        <button type="submit" disabled={busy}>
          {busy
            ? 'Please wait…'
            : mode === 'login'
              ? 'Sign in'
              : mode === 'onboard'
                ? 'Create account'
                : mode === 'reset'
                  ? 'Send reset email'
                  : 'Change password'}
        </button>
      </form>
      <nav aria-label="Account options" className="local-account-links">
        {mode !== 'login' && (
          <button
            type="button"
            onClick={() => {
              setMode('login')
              resetMessage()
            }}
          >
            Sign in
          </button>
        )}
        {mode !== 'onboard' && (
          <button
            type="button"
            onClick={() => {
              setMode('onboard')
              resetMessage()
            }}
          >
            Create invite account
          </button>
        )}
        {mode !== 'reset' && (
          <button
            type="button"
            onClick={() => {
              setMode('reset')
              resetMessage()
            }}
          >
            Forgot password
          </button>
        )}
      </nav>
      <button
        type="button"
        className="local-account-github"
        onClick={() => void beginGithubOnboarding()}
      >
        Use GitHub with an invite
      </button>
      {onSignedOut && (
        <button type="button" onClick={onSignedOut}>
          Sign out
        </button>
      )}
    </section>
  )
}
