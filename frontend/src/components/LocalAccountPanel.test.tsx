import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { LocalAccountPanel } from './LocalAccountPanel'

function response(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': 'test-token' },
  })
}

describe('LocalAccountPanel', () => {
  beforeEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('creates an invite-gated account without rendering a password', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(response({ player_id: '1' }, 201))
    render(<LocalAccountPanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Create invite account' }))
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'rider' } })
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'rider@example.com' } })
    fireEvent.change(screen.getByLabelText('Competition invite code'), {
      target: { value: 'ABC123' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Check your email'))
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/game/auth/local/onboard/'),
      expect.objectContaining({ method: 'POST' }),
    )
    expect(screen.queryByText(/temporary password is/i)).not.toBeInTheDocument()
  })

  it('forces a temporary-password change after local login', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      response({ player_id: '1', must_change_password: true }),
    )
    render(<LocalAccountPanel />)
    fireEvent.change(screen.getByLabelText('Username or email'), { target: { value: 'rider' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'temporary-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.getByLabelText('New password')).toBeInTheDocument())
    expect(screen.getByRole('status')).toHaveTextContent('Change your temporary password')
  })

  it('requires an attestation before upload controls become active', () => {
    render(<LocalAccountPanel authenticated />)
    const input = screen.getByLabelText('Activity files')
    const submit = screen.getByRole('button', { name: 'Upload activities' })
    const file = new File(['<gpx />'], 'ride.gpx', { type: 'application/gpx+xml' })
    fireEvent.change(input, { target: { files: [file] } })
    expect(submit).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    expect(submit).not.toBeDisabled()
  })
})
