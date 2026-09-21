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
    vi.spyOn(apiClient, 'GET').mockResolvedValue(result(undefined, 503) as never)
    render(<GameCompetitions copy={translations.en} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be reached/i)
  })
})
