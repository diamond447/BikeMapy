import { useCallback, useEffect, useState } from 'react'

import { apiClient, csrfHeaders, rememberCsrfToken } from '../api/client'
import type { components } from '../api/generated/schema'
import { notifyGameDataRefresh, notifyGameMapReset } from '../gameState'
import type { Copy } from '../i18n/types'

type Competition = components['schemas']['Competition']
type RosterMember = components['schemas']['CompetitionRosterMember']

function errorDetail(error: unknown): string | null {
  if (!error || typeof error !== 'object' || !('detail' in error)) return null
  return typeof error.detail === 'string' ? error.detail : null
}

export function GameCompetitions({ copy }: { copy: Copy }) {
  const [competitions, setCompetitions] = useState<Competition[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [invite, setInvite] = useState('')
  const [newColor, setNewColor] = useState('#E45756')
  const [renameId, setRenameId] = useState<string | null>(null)
  const [rename, setRename] = useState('')
  const [sharingScope, setSharingScope] = useState<'recent' | 'full_history'>('recent')
  const [sharingConfirmed, setSharingConfirmed] = useState(false)
  const [roster, setRoster] = useState<RosterMember[]>([])
  const [rosterCursor, setRosterCursor] = useState<string | null>(null)
  const [rosterLoading, setRosterLoading] = useState(false)
  const [rosterSearch, setRosterSearch] = useState('')
  const [rosterReload, setRosterReload] = useState(0)

  const load = useCallback(async (): Promise<boolean> => {
    setLoading(true)
    setLoadError(false)
    try {
      const result = await apiClient.GET('/api/v1/game/competitions/', { credentials: 'include' })
      rememberCsrfToken(result.response)
      if (result.data && Array.isArray(result.data.competitions)) {
        setCompetitions(result.data.competitions)
        const selectedScope = result.data.competitions.find(
          (competition) => competition.is_selected,
        )?.sharing_scope
        if (selectedScope === 'recent' || selectedScope === 'full_history') {
          setSharingScope(selectedScope)
        }
        setSharingConfirmed(false)
        return true
      }
      if (result.response?.status === 401) notifyGameMapReset('auth-loss')
      if (result.response?.status !== 401) {
        setMessage(errorDetail(result.error) ?? copy.gameCompetitionError)
        setLoadError(true)
      }
      return false
    } catch {
      setMessage(copy.gameCompetitionError)
      setLoadError(true)
      return false
    } finally {
      setLoading(false)
    }
  }, [copy.gameCompetitionError])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const current = competitions.find((competition) => competition.is_selected) ?? competitions[0]
  const currentId = current?.id
  const currentMembersTruncated = current?.members_truncated

  const loadRoster = useCallback(
    async (reset: boolean, search = '', cursor: string | null = null): Promise<void> => {
      if (!currentId || (!currentMembersTruncated && reset)) {
        return
      }
      setRosterLoading(true)
      try {
        const result = await apiClient.GET('/api/v1/game/competitions/{competition_id}/members/', {
          params: {
            path: { competition_id: currentId },
            query: {
              cursor: reset ? undefined : (cursor ?? undefined),
              search: search.trim() || undefined,
            },
          },
          credentials: 'include',
        })
        if (result.data) {
          setRoster((previous) => {
            const members = reset ? result.data.members : [...previous, ...result.data.members]
            return [...new Map(members.map((member) => [member.player_id, member])).values()]
          })
          setRosterCursor(result.data.next_cursor)
        }
      } catch {
        // The competition panel remains usable with its bounded initial list.
      } finally {
        setRosterLoading(false)
      }
    },
    [currentId, currentMembersTruncated],
  )

  useEffect(() => {
    if (!currentMembersTruncated) return
    const timer = window.setTimeout(() => void loadRoster(true), 0)
    return () => window.clearTimeout(timer)
  }, [currentId, currentMembersTruncated, loadRoster, rosterReload])

  const action = async (
    run: () => Promise<{ response?: Response; data?: unknown; error?: unknown }>,
  ): Promise<boolean> => {
    notifyGameMapReset('competition-change')
    setBusy(true)
    setMessage(null)
    try {
      const result = await run()
      rememberCsrfToken(result.response)
      if (result.response?.status && result.response.status >= 400) {
        if (result.response.status === 401) notifyGameMapReset('auth-loss')
        setMessage(errorDetail(result.error) ?? copy.gameCompetitionError)
        return false
      } else {
        setRoster([])
        setRosterCursor(null)
        const reloaded = await load()
        if (!reloaded) setMessage(copy.gameCompetitionReloadError)
        else {
          setRosterReload((value) => value + 1)
          notifyGameDataRefresh()
        }
        return true
      }
    } catch {
      setMessage(copy.gameCompetitionError)
      return false
    } finally {
      setBusy(false)
    }
  }

  const create = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (
      await action(() =>
        apiClient.POST('/api/v1/game/competitions/', {
          body: { name: name.trim(), color: newColor },
          credentials: 'include',
          headers: csrfHeaders(),
        }),
      )
    )
      setName('')
  }

  const join = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (
      await action(() =>
        apiClient.POST('/api/v1/game/competitions/join/', {
          body: { invite_code: invite.trim(), color: newColor },
          credentials: 'include',
          headers: csrfHeaders(),
        }),
      )
    )
      setInvite('')
  }

  const managedMembers = current
    ? [
        ...current.members,
        ...roster.filter(
          (member) =>
            !current.members.some((currentMember) => currentMember.player_id === member.player_id),
        ),
      ]
    : []

  if (loading) return <p className="game-account-status">{copy.gameCompetitionsLoading}</p>

  if (loadError)
    return (
      <div className="game-account-message">
        <p role="alert">{message ?? copy.gameCompetitionError}</p>
        <button type="button" className="game-account-primary" onClick={() => void load()}>
          {copy.gameCompetitionRetry}
        </button>
      </div>
    )

  return (
    <section className="game-competitions" aria-labelledby="game-competitions-heading">
      <div className="game-competitions-heading">
        <div>
          <span className="game-account-kicker">{copy.gameCompetitionKicker}</span>
          <h3 id="game-competitions-heading">{copy.gameCompetitions}</h3>
        </div>
        <span className="game-competition-count">{competitions.length}</span>
      </div>
      {message && (
        <p className="game-account-feedback" role="alert">
          {message}
        </p>
      )}
      {current && (
        <div className="game-competition-switcher" aria-label={copy.gameCompetitionSwitcher}>
          {competitions.map((competition) => (
            <button
              type="button"
              key={competition.id}
              className={competition.id === current.id ? 'is-selected' : ''}
              onClick={() =>
                void action(() =>
                  apiClient.POST('/api/v1/game/competitions/{competition_id}/switch/', {
                    params: { path: { competition_id: competition.id } },
                    credentials: 'include',
                    headers: csrfHeaders(),
                  }),
                )
              }
              disabled={busy || competition.id === current.id}
            >
              <span
                className="game-competition-swatch"
                style={{ backgroundColor: competition.color }}
                aria-hidden="true"
              />
              <span>{competition.name}</span>
            </button>
          ))}
        </div>
      )}
      <div className="game-competition-forms">
        <form onSubmit={create}>
          <label htmlFor="game-competition-name">{copy.gameCompetitionCreate}</label>
          <input
            id="game-competition-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={120}
            placeholder={copy.gameCompetitionNamePlaceholder}
          />
          <label className="game-competition-color" htmlFor="game-competition-new-color">
            {copy.gameCompetitionColor}
            <input
              id="game-competition-new-color"
              type="color"
              value={newColor}
              onChange={(event) => setNewColor(event.target.value)}
            />
          </label>
          <button type="submit" className="game-account-primary" disabled={busy || !name.trim()}>
            {copy.gameCompetitionCreate}
          </button>
        </form>
        <form onSubmit={join}>
          <label htmlFor="game-competition-invite">{copy.gameCompetitionJoin}</label>
          <input
            id="game-competition-invite"
            value={invite}
            onChange={(event) => setInvite(event.target.value.toUpperCase())}
            maxLength={32}
            placeholder={copy.gameCompetitionInvitePlaceholder}
          />
          <button type="submit" className="game-account-primary" disabled={busy || !invite.trim()}>
            {copy.gameCompetitionJoin}
          </button>
        </form>
      </div>
      {current && (
        <div className="game-competition-management">
          <div className="game-competition-sharing">
            <span className="game-competition-label">{copy.gameCompetitionSharing}</span>
            <span>
              {current.sharing_scope === 'none'
                ? copy.gameCompetitionSharingOff
                : copy.gameCompetitionSharingOn}
            </span>
            <p className="game-competition-sharing-disclosure">
              {copy.gameCompetitionSharingDisclosure}
            </p>
            <label htmlFor="game-competition-sharing-scope">
              {copy.gameCompetitionSharingScope}
              <select
                id="game-competition-sharing-scope"
                value={sharingScope}
                onChange={(event) =>
                  setSharingScope(event.target.value as 'recent' | 'full_history')
                }
                disabled={busy}
              >
                <option value="recent">{copy.gameCompetitionSharingRecent}</option>
                <option value="full_history">{copy.gameCompetitionSharingFullHistory}</option>
              </select>
            </label>
            <label>
              <input
                type="checkbox"
                checked={sharingConfirmed}
                onChange={(event) => setSharingConfirmed(event.target.checked)}
                disabled={busy}
              />
              {copy.gameCompetitionSharingConfirm}
            </label>
            <button
              type="button"
              onClick={() =>
                void action(() =>
                  apiClient.POST('/api/v1/game/competitions/{competition_id}/sharing-consent/', {
                    params: { path: { competition_id: current.id } },
                    body: {
                      scope: sharingScope,
                      disclosure_version: '2026-09-25',
                      confirmed: sharingConfirmed,
                    },
                    credentials: 'include',
                    headers: csrfHeaders(),
                  }),
                )
              }
              disabled={busy || !sharingConfirmed}
            >
              {current.sharing_scope === 'none'
                ? copy.gameCompetitionSharingEnable
                : copy.gameCompetitionSharingUpdate}
            </button>
            {current.sharing_scope !== 'none' && (
              <button
                type="button"
                onClick={() =>
                  void action(() =>
                    apiClient.DELETE(
                      '/api/v1/game/competitions/{competition_id}/sharing-consent/',
                      {
                        params: { path: { competition_id: current.id } },
                        credentials: 'include',
                        headers: csrfHeaders(),
                      },
                    ),
                  )
                }
                disabled={busy}
              >
                {copy.gameCompetitionSharingWithdraw}
              </button>
            )}
          </div>
          <div className="game-competition-code">
            <span>{copy.gameCompetitionInviteCode}</span>
            <code>{current.invite_code}</code>
            {current.is_owner && (
              <button
                type="button"
                onClick={() =>
                  void action(() =>
                    apiClient.POST('/api/v1/game/competitions/{competition_id}/rotate-invite/', {
                      params: { path: { competition_id: current.id } },
                      credentials: 'include',
                      headers: csrfHeaders(),
                    }),
                  )
                }
                disabled={busy}
              >
                {copy.gameCompetitionRotate}
              </button>
            )}
          </div>
          <label className="game-competition-color" htmlFor="game-competition-color">
            {copy.gameCompetitionColor}
            <input
              id="game-competition-color"
              type="color"
              value={current.color}
              onChange={(event) =>
                void action(() =>
                  apiClient.PATCH('/api/v1/game/competitions/{competition_id}/members/me/', {
                    params: { path: { competition_id: current.id } },
                    body: { color: event.target.value },
                    credentials: 'include',
                    headers: csrfHeaders(),
                  }),
                )
              }
              disabled={busy}
            />
          </label>
          {current.is_owner ? (
            <div className="game-competition-members">
              <span className="game-competition-label">{copy.gameCompetitionMembers}</span>
              {managedMembers.map((member) => (
                <div className="game-competition-member" key={member.player_id}>
                  <span
                    className="game-competition-swatch"
                    style={{ backgroundColor: member.color }}
                    aria-hidden="true"
                  />
                  <span>{member.nickname ?? member.display_name}</span>
                  {member.is_owner ? (
                    <small>{copy.gameCompetitionOwner}</small>
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={() =>
                          void action(() =>
                            apiClient.POST('/api/v1/game/competitions/{competition_id}/transfer/', {
                              params: { path: { competition_id: current.id } },
                              body: { player_id: member.player_id },
                              credentials: 'include',
                              headers: csrfHeaders(),
                            }),
                          )
                        }
                        disabled={busy}
                      >
                        {copy.gameCompetitionTransfer}
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          void action(() =>
                            apiClient.DELETE(
                              '/api/v1/game/competitions/{competition_id}/members/{player_id}/',
                              {
                                params: {
                                  path: { competition_id: current.id, player_id: member.player_id },
                                },
                                credentials: 'include',
                                headers: csrfHeaders(),
                              },
                            ),
                          )
                        }
                        disabled={busy}
                      >
                        {copy.gameCompetitionRemove}
                      </button>
                    </>
                  )}
                </div>
              ))}
              {current.members_truncated && (
                <div className="game-competition-roster-tools">
                  <label htmlFor="game-competition-member-search">
                    {copy.gameCompetitionMemberSearch}
                  </label>
                  <input
                    id="game-competition-member-search"
                    inputMode="numeric"
                    value={rosterSearch}
                    onChange={(event) => setRosterSearch(event.target.value)}
                    placeholder={copy.gameCompetitionMemberSearchPlaceholder}
                  />
                  <button
                    type="button"
                    onClick={() => void loadRoster(true, rosterSearch)}
                    disabled={busy || rosterLoading}
                  >
                    {copy.gameCompetitionMemberSearchAction}
                  </button>
                  {rosterCursor && !rosterSearch.trim() && (
                    <button
                      type="button"
                      onClick={() => void loadRoster(false, '', rosterCursor)}
                      disabled={busy || rosterLoading}
                    >
                      {copy.gameCompetitionMemberLoadMore}
                    </button>
                  )}
                </div>
              )}
            </div>
          ) : (
            <button
              type="button"
              className="game-competition-leave"
              onClick={() =>
                void action(() =>
                  apiClient.POST('/api/v1/game/competitions/{competition_id}/leave/', {
                    params: { path: { competition_id: current.id } },
                    credentials: 'include',
                    headers: csrfHeaders(),
                  }),
                )
              }
              disabled={busy}
            >
              {copy.gameCompetitionLeave}
            </button>
          )}
          {current.is_owner && (
            <div className="game-competition-owner-actions">
              {renameId === current.id ? (
                <form
                  onSubmit={async (event) => {
                    event.preventDefault()
                    const succeeded = await action(() =>
                      apiClient.PATCH('/api/v1/game/competitions/{competition_id}/', {
                        params: { path: { competition_id: current.id } },
                        body: { name: rename },
                        credentials: 'include',
                        headers: csrfHeaders(),
                      }),
                    )
                    if (succeeded) setRenameId(null)
                  }}
                >
                  <input
                    aria-label={copy.gameCompetitionRename}
                    value={rename}
                    onChange={(event) => setRename(event.target.value)}
                    maxLength={120}
                  />
                  <button type="submit" disabled={busy}>
                    {copy.gameSaveNickname}
                  </button>
                </form>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setRenameId(current.id)
                    setRename(current.name)
                  }}
                  disabled={busy}
                >
                  {copy.gameCompetitionRename}
                </button>
              )}
              <button
                type="button"
                className="game-account-danger"
                onClick={() =>
                  void action(() =>
                    apiClient.DELETE('/api/v1/game/competitions/{competition_id}/', {
                      params: { path: { competition_id: current.id } },
                      credentials: 'include',
                      headers: csrfHeaders(),
                    }),
                  )
                }
                disabled={busy}
              >
                {copy.gameCompetitionDelete}
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
