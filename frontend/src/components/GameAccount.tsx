import { useCallback, useEffect, useMemo, useState } from 'react'

import { apiBaseUrl, apiClient, csrfHeaders, rememberCsrfToken } from '../api/client'
import type { components } from '../api/generated/schema'
import type { Copy } from '../i18n/types'
import { GameCompetitions } from './GameCompetitions'

type Player = components['schemas']['Player']
type PlayerResponse = components['schemas']['PlayerResponse']
type AccountStatus = 'checking' | 'unavailable' | 'signed-out' | 'authenticated' | 'error'

function isPlayerResponse(value: unknown): value is PlayerResponse {
  if (!value || typeof value !== 'object' || !('player' in value)) return false
  const player = value.player
  return Boolean(
    player &&
    typeof player === 'object' &&
    'display_name' in player &&
    typeof player.display_name === 'string',
  )
}

function errorDetail(error: unknown): string | null {
  if (!error || typeof error !== 'object' || !('detail' in error)) return null
  return typeof error.detail === 'string' ? error.detail : null
}

function accountEndpoint(path: string) {
  return `${apiBaseUrl.replace(/\/$/, '')}${path}`
}

export function GameAccount({ copy, initialOpen = false }: { copy: Copy; initialOpen?: boolean }) {
  const [open, setOpen] = useState(initialOpen)
  const [status, setStatus] = useState<AccountStatus>('unavailable')
  const [player, setPlayer] = useState<Player | null>(null)
  const [nickname, setNickname] = useState('')
  const [busy, setBusy] = useState(false)
  const [retryToken, setRetryToken] = useState(0)
  const [message, setMessage] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null
    const params = new URLSearchParams(window.location.search)
    const result = params.get('game_auth') ?? params.get('error')
    if (!result || result === 'success') return null
    return result === 'denied' || result === 'access_denied'
      ? copy.gameAuthDenied
      : copy.gameAuthError
  })

  const loadSession = useCallback(async () => {
    setStatus('checking')
    const result = await apiClient.GET('/api/v1/game/auth/session/', {
      credentials: 'include',
    })
    rememberCsrfToken(result.response)
    if (result.response?.status === 404) {
      setPlayer(null)
      setStatus('unavailable')
      return
    }
    if (result.response?.status === 401 || !result.data) {
      setPlayer(null)
      setStatus(result.response?.status === 401 ? 'signed-out' : 'error')
      if (result.response?.status !== 401) setMessage(copy.gameSessionError)
      return
    }
    if (!isPlayerResponse(result.data)) {
      setPlayer(null)
      setStatus('signed-out')
      return
    }
    setPlayer(result.data.player)
    setNickname(result.data.player.nickname ?? '')
    setStatus('authenticated')
  }, [copy.gameSessionError])

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => void loadSession(), 0)
    return () => window.clearTimeout(timer)
  }, [loadSession, open, retryToken])

  const signInUrl = useMemo(() => accountEndpoint('/api/v1/game/auth/strava/authorize/'), [])

  const runAccountAction = useCallback(
    async (action: () => Promise<{ response?: Response; data?: unknown; error?: unknown }>) => {
      setBusy(true)
      setMessage(null)
      try {
        return await action()
      } catch {
        setStatus('error')
        setMessage(copy.gameSessionError)
        return null
      } finally {
        setBusy(false)
      }
    },
    [copy.gameSessionError],
  )

  const saveNickname = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const result = await runAccountAction(() =>
      apiClient.PATCH('/api/v1/game/account/', {
        body: { nickname: nickname.trim() },
        credentials: 'include',
        headers: csrfHeaders(),
      }),
    )
    if (!result) return
    if (result.response?.status === 401) {
      setPlayer(null)
      setStatus('signed-out')
      setMessage(copy.gameRefreshFailure)
    } else if (isPlayerResponse(result.data)) {
      setPlayer(result.data.player)
      setNickname(result.data.player.nickname ?? '')
      setMessage(copy.gameSaved)
      setStatus('authenticated')
    } else {
      setMessage(errorDetail(result.error) ?? copy.gameSessionError)
    }
  }

  const refresh = async () => {
    const result = await runAccountAction(() =>
      apiClient.POST('/api/v1/game/account/refresh/', {
        credentials: 'include',
        headers: csrfHeaders(),
      }),
    )
    if (!result) return
    if (result.response?.status === 401) {
      setPlayer(null)
      setStatus('signed-out')
      setMessage(copy.gameRefreshFailure)
    } else if (isPlayerResponse(result.data)) {
      setPlayer(result.data.player)
      setNickname(result.data.player.nickname ?? '')
      setMessage(copy.gameRefreshSuccess)
      setStatus('authenticated')
    } else {
      setMessage(errorDetail(result.error) ?? copy.gameSessionError)
    }
  }

  const disconnect = async () => {
    const result = await runAccountAction(() =>
      apiClient.POST('/api/v1/game/account/disconnect/', {
        credentials: 'include',
        headers: csrfHeaders(),
      }),
    )
    if (!result) return
    if (result.response?.status === 401) {
      setPlayer(null)
      setStatus('signed-out')
      setMessage(copy.gameRefreshFailure)
    } else if (result.response?.status !== 200) {
      setStatus(result.response?.status === 404 ? 'unavailable' : 'error')
      setMessage(errorDetail(result.error) ?? copy.gameSessionError)
    } else {
      setPlayer(null)
      setStatus('signed-out')
      setMessage(copy.gameDisconnected)
    }
  }

  const logout = async () => {
    const result = await runAccountAction(() =>
      apiClient.POST('/api/v1/game/auth/logout/', {
        credentials: 'include',
        headers: csrfHeaders(),
      }),
    )
    if (!result) return
    setPlayer(null)
    setStatus('signed-out')
    setMessage(copy.gameLoggedOut)
  }

  const deleteAccount = async () => {
    if (!window.confirm(`${copy.gameDeleteTitle}\n\n${copy.gameDeleteDescription}`)) return
    const result = await runAccountAction(() =>
      apiClient.DELETE('/api/v1/game/account/', {
        credentials: 'include',
        headers: csrfHeaders(),
      }),
    )
    if (!result) return
    if (result.response?.status === 401) {
      setPlayer(null)
      setStatus('signed-out')
      setMessage(copy.gameRefreshFailure)
      return
    }
    if (result.response?.status !== 204) {
      setStatus(result.response?.status === 404 ? 'unavailable' : 'error')
      setMessage(errorDetail(result.error) ?? copy.gameSessionError)
      return
    }
    setPlayer(null)
    setStatus('signed-out')
    setMessage(copy.gameDeleted)
  }

  const renderContent = () => {
    if (status === 'checking') return <p className="game-account-status">{copy.gameChecking}</p>
    if (status === 'unavailable') {
      return (
        <div className="game-account-message">
          <h2>{copy.gameUnavailable}</h2>
          <p>{copy.gameUnavailableDescription}</p>
        </div>
      )
    }
    if (status === 'error') {
      return (
        <div className="game-account-message">
          <h2>{copy.gameAccount}</h2>
          <p role="alert">{message ?? copy.gameSessionError}</p>
          <button
            type="button"
            className="game-account-primary"
            onClick={() => setRetryToken((value) => value + 1)}
          >
            {copy.gameRetry}
          </button>
        </div>
      )
    }
    if (status === 'signed-out' || !player) {
      return (
        <div className="game-account-message">
          <h2>{copy.gameAccount}</h2>
          {message && <p role="status">{message}</p>}
          <p>{copy.gameSignIn}</p>
          <a className="game-account-primary" href={signInUrl}>
            {message === copy.gameRefreshFailure ? copy.gameReconnect : copy.gameSignInStrava}
          </a>
        </div>
      )
    }
    return (
      <div className="game-account-content">
        <div className="game-account-identity">
          {player.profile_image_url ? (
            <img src={player.profile_image_url} alt="" />
          ) : (
            <span className="game-account-avatar" aria-hidden="true">
              {player.display_name.slice(0, 1).toUpperCase()}
            </span>
          )}
          <div>
            <strong>{player.display_name}</strong>
            <span className="game-account-chip">{copy.gameConnected}</span>
          </div>
        </div>
        {message && (
          <p className="game-account-feedback" role="status">
            {message}
          </p>
        )}
        <form className="game-account-form" onSubmit={saveNickname}>
          <label htmlFor="game-nickname">{copy.gameNickname}</label>
          <input
            id="game-nickname"
            value={nickname}
            maxLength={80}
            onChange={(event) => setNickname(event.target.value)}
            aria-describedby="game-nickname-hint"
          />
          <small id="game-nickname-hint">{copy.gameNicknameHint}</small>
          <button type="submit" className="game-account-primary" disabled={busy}>
            {busy ? copy.gameSaving : copy.gameSaveNickname}
          </button>
        </form>
        <dl className="game-account-details">
          <div>
            <dt>{copy.gameDisplayName}</dt>
            <dd>{player.display_name}</dd>
          </div>
        </dl>
        <GameCompetitions copy={copy} />
        <div className="game-account-actions">
          <button type="button" onClick={() => void refresh()} disabled={busy}>
            {busy ? copy.gameRefreshing : copy.gameRefresh}
          </button>
          <button type="button" onClick={() => void disconnect()} disabled={busy}>
            {copy.gameDisconnect}
          </button>
          <button type="button" onClick={() => void logout()} disabled={busy}>
            {copy.gameLogout}
          </button>
          <button
            type="button"
            className="game-account-danger"
            onClick={() => void deleteAccount()}
            disabled={busy}
          >
            {copy.gameDelete}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="game-account-dock">
      <button
        type="button"
        className={`game-account-trigger game-account-trigger-${status}`}
        aria-expanded={open}
        aria-controls="game-account-panel"
        aria-label={copy.gameLabel}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="game-account-trigger-mark" aria-hidden="true">
          {status === 'authenticated' && player
            ? player.display_name.slice(0, 1).toUpperCase()
            : '↗'}
        </span>
        <span>{copy.gameLabel}</span>
      </button>
      {open && (
        <aside id="game-account-panel" className="game-account-panel" aria-label={copy.gameAccount}>
          <div className="game-account-panel-heading">
            <span className="game-account-kicker">{copy.gameLabel}</span>
            <button type="button" className="game-account-close" onClick={() => setOpen(false)}>
              <span className="sr-only">{copy.gameClose}</span>×
            </button>
          </div>
          {renderContent()}
        </aside>
      )}
    </div>
  )
}
