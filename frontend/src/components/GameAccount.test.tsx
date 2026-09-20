import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../api/client'
import { translations } from '../i18n/translations'
import { GameAccount } from './GameAccount'

const player = {
  id: 'player-1',
  athlete_id: '42',
  display_name: 'Ada Lovelace',
  profile_image_url: null,
  nickname: 'Ada',
  lifecycle: 'connected',
  connected_at: '2026-09-20T09:00:00Z',
} as const

function response(status: number) {
  return new Response(null, { status })
}

describe('GameAccount', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/game')
    document.cookie = 'csrftoken=test-csrf-token'
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('keeps the public-facing account entry safe when the private game is disabled', async () => {
    vi.spyOn(apiClient, 'GET').mockResolvedValue({
      data: undefined,
      error: { detail: 'The private game is unavailable.' },
      response: response(404),
    } as never)

    render(<GameAccount copy={translations.en} initialOpen />)

    expect(
      await screen.findByRole('heading', { name: /private game unavailable/i }),
    ).toBeInTheDocument()
    expect(screen.getByText(/public route catalogue remains available/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^player account$/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('presents an unauthenticated and denied callback as a safe retry path', async () => {
    window.history.replaceState({}, '', '/game?game_auth=denied')
    vi.spyOn(apiClient, 'GET').mockResolvedValue({
      data: undefined,
      error: { detail: 'Player authentication is required.' },
      response: response(401),
    } as never)

    render(<GameAccount copy={translations.en} initialOpen />)

    expect(await screen.findByText(/authorization was not completed/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /sign in with strava/i })).toHaveAttribute(
      'href',
      'http://localhost:8000/api/v1/game/auth/strava/authorize/',
    )
  })

  it('edits the nickname and logs out the current player', async () => {
    const get = vi.spyOn(apiClient, 'GET').mockResolvedValue({
      data: { player },
      error: undefined,
      response: response(200),
    } as never)
    const patch = vi.spyOn(apiClient, 'PATCH').mockResolvedValue({
      data: { player: { ...player, nickname: 'Trail Ada' } },
      error: undefined,
      response: response(200),
    } as never)
    const post = vi.spyOn(apiClient, 'POST').mockResolvedValue({
      data: undefined,
      error: undefined,
      response: response(204),
    } as never)
    const user = userEvent.setup()

    render(<GameAccount copy={translations.en} initialOpen />)
    const nickname = await screen.findByLabelText(/bikemapy nickname/i)
    await user.clear(nickname)
    await user.type(nickname, 'Trail Ada')
    await user.click(screen.getByRole('button', { name: /save nickname/i }))

    await waitFor(() => expect(patch).toHaveBeenCalled())
    expect(patch.mock.calls[0]?.[1]).toMatchObject({
      body: { nickname: 'Trail Ada' },
      headers: { 'X-CSRFToken': 'test-csrf-token' },
    })
    expect(await screen.findByText('Nickname saved.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^log out$/i }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/v1/game/auth/logout/', expect.anything()),
    )
    expect(await screen.findByText(/you are logged out/i)).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/api/v1/game/auth/session/', expect.anything())
  })

  it('requires confirmation before deleting an account and handles refresh loss', async () => {
    vi.spyOn(apiClient, 'GET').mockResolvedValue({
      data: { player },
      error: undefined,
      response: response(200),
    } as never)
    const post = vi.spyOn(apiClient, 'POST').mockResolvedValue({
      data: undefined,
      error: { detail: 'Strava connection is no longer valid.' },
      response: response(401),
    } as never)
    const remove = vi.spyOn(apiClient, 'DELETE').mockResolvedValue({
      data: undefined,
      error: undefined,
      response: response(204),
    } as never)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const user = userEvent.setup()

    render(<GameAccount copy={translations.en} initialOpen />)
    await screen.findByLabelText(/bikemapy nickname/i)
    await user.click(screen.getByRole('button', { name: /delete player account/i }))
    expect(confirm).toHaveBeenCalled()
    expect(remove).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /refresh strava connection/i }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/v1/game/account/refresh/', expect.anything()),
    )
    expect(await screen.findByText(/needs attention/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /reconnect with strava/i })).toBeInTheDocument()
  })
})
