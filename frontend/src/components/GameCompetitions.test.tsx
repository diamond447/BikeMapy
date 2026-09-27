import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../api/client'
import { translations } from '../i18n/translations'
import { GameCompetitions } from './GameCompetitions'

const competition = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'Dawn rides',
  invite_code: 'DAWN234567',
  owner_player_id: 1,
  is_owner: true,
  is_active: true,
  is_selected: true,
  color: '#E45756',
  created_at: '2026-09-21T09:00:00Z',
  members: [
    { player_id: 1, display_name: 'Ada', nickname: 'Ada', color: '#E45756', is_owner: true },
    { player_id: 2, display_name: 'Linus', nickname: null, color: '#3A86FF', is_owner: false },
  ],
} as const

function result(data: unknown, status = 200) {
  return { data, error: undefined, response: new Response(null, { status }) }
}

describe('GameCompetitions', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('creates, joins, switches, edits, rotates, removes and deletes competitions', async () => {
    const get = vi
      .spyOn(apiClient, 'GET')
      .mockResolvedValue(
        result({ competitions: [competition], active_competition_id: competition.id }) as never,
      )
    const post = vi.spyOn(apiClient, 'POST').mockResolvedValue(result({ competition }) as never)
    const patch = vi.spyOn(apiClient, 'PATCH').mockResolvedValue(result({ competition }) as never)
    const remove = vi.spyOn(apiClient, 'DELETE').mockResolvedValue(result(undefined, 204) as never)
    const user = userEvent.setup()

    render(<GameCompetitions copy={translations.en} />)
    expect(await screen.findByRole('heading', { name: 'Competitions' })).toBeInTheDocument()
    await user.type(screen.getByLabelText('Create competition'), 'Rain loop')
    await user.click(screen.getByRole('button', { name: 'Create competition' }))
    await user.type(screen.getByPlaceholderText('Enter invite code'), 'JOIN234567')
    await user.click(screen.getByRole('button', { name: 'Join competition' }))
    await user.click(screen.getByRole('button', { name: /rotate/i }))
    fireEvent.change(document.querySelector('#game-competition-color')!, {
      target: { value: '#00ff00' },
    })
    await user.click(screen.getByRole('button', { name: 'Make owner' }))
    await user.click(screen.getByRole('button', { name: 'Remove' }))
    await user.click(screen.getByRole('button', { name: 'Rename' }))
    const rename = screen.getByRole('textbox', { name: 'Rename' })
    await user.clear(rename)
    await user.type(rename, 'New name')
    await user.click(screen.getByRole('button', { name: 'Save nickname' }))
    await user.click(screen.getByRole('button', { name: 'Delete competition' }))

    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(post).toHaveBeenCalled()
    expect(patch).toHaveBeenCalled()
    expect(remove).toHaveBeenCalled()
  })

  it('shows a safe error when competition loading fails', async () => {
    const get = vi
      .spyOn(apiClient, 'GET')
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue(result({ competitions: [], active_competition_id: null }) as never)
    render(<GameCompetitions copy={translations.en} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be reached/i)
    await userEvent.setup().click(screen.getByRole('button', { name: /retry competitions/i }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })

  it('preserves create and join fields when a mutation fails', async () => {
    vi.spyOn(apiClient, 'GET').mockResolvedValue(
      result({ competitions: [], active_competition_id: null }) as never,
    )
    vi.spyOn(apiClient, 'POST').mockRejectedValue(new Error('network down'))
    const user = userEvent.setup()
    render(<GameCompetitions copy={translations.en} />)
    const name = await screen.findByLabelText('Create competition')
    const invite = screen.getByPlaceholderText('Enter invite code')
    await user.type(name, 'Rain loop')
    await user.click(screen.getByRole('button', { name: 'Create competition' }))
    expect(name).toHaveValue('Rain loop')
    await user.type(invite, 'JOIN123')
    await user.click(screen.getByRole('button', { name: 'Join competition' }))
    expect(invite).toHaveValue('JOIN123')
  })

  it('loads and manages members beyond the legacy first page', async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      player_id: index + 1,
      display_name: `Rider ${index + 1}`,
      nickname: null,
      color: '#3A86FF',
      is_owner: index === 0,
      sharing_active: true,
    }))
    const lateMember = {
      player_id: 101,
      display_name: 'Rider 101',
      nickname: null,
      color: '#E45756',
      is_owner: false,
      sharing_active: true,
    }
    const largeCompetition = {
      ...competition,
      members: firstPage,
      members_truncated: false,
      roster_count: 101,
      roster_truncated: true,
    }
    const get = vi.spyOn(apiClient, 'GET').mockImplementation(((
      path: string,
      options?: unknown,
    ) => {
      if (path.includes('/members/')) {
        const cursor = String(
          (options as { params?: { query?: { cursor?: string } } } | undefined)?.params?.query
            ?.cursor ?? '',
        )
        return Promise.resolve(
          result(
            cursor
              ? { members: [lateMember], next_cursor: null, has_more: false }
              : { members: firstPage, next_cursor: 'next', has_more: true },
          ) as never,
        )
      }
      return Promise.resolve(
        result({
          competitions: [largeCompetition],
          active_competition_id: competition.id,
        }) as never,
      )
    }) as never)
    const remove = vi.spyOn(apiClient, 'DELETE').mockResolvedValue(result(undefined, 204) as never)
    render(<GameCompetitions copy={translations.en} />)
    expect(await screen.findByText('Rider 1')).toBeInTheDocument()
    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: 'Load more members' }, { timeout: 5000 }))
    expect(await screen.findByText('Rider 101')).toBeInTheDocument()
    const removeButtons = screen.getAllByRole('button', { name: 'Remove' })
    await userEvent.setup().click(removeButtons[removeButtons.length - 1]!)
    await waitFor(() =>
      expect(remove).toHaveBeenCalledWith(
        '/api/v1/game/competitions/{competition_id}/members/{player_id}/',
        expect.objectContaining({
          params: { path: { competition_id: competition.id, player_id: 101 } },
        }),
      ),
    )
    expect(get).toHaveBeenCalledWith(
      '/api/v1/game/competitions/{competition_id}/members/',
      expect.anything(),
    )
  })
})
