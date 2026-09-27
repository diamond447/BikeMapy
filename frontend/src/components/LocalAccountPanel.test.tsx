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

  it('signs in with a permanent local password', async () => {
    const onAuthenticated = vi.fn()
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(response({ player_id: '1', must_change_password: false }))
    render(<LocalAccountPanel onAuthenticated={onAuthenticated} />)
    fireEvent.change(screen.getByLabelText('Username or email'), {
      target: { value: 'rider' },
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'permanent-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Signed in.'))
    expect(onAuthenticated).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/game/auth/local/login/'),
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('sends a password reset request without disclosing account state', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({}, 202))
    render(<LocalAccountPanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Forgot password' }))
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'rider@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send reset email' }))
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('If the account exists'),
    )
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/game/auth/local/reset/'),
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('requires an invite before starting GitHub onboarding', () => {
    render(<LocalAccountPanel />)
    fireEvent.click(screen.getByRole('button', { name: 'Use GitHub with an invite' }))
    expect(screen.getByRole('status')).toHaveTextContent('Enter your competition invite code')
    expect(screen.getByLabelText('Username')).toBeInTheDocument()
  })

  it('changes a temporary password and reports the authenticated state', async () => {
    const onAuthenticated = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      response({ player_id: '1', must_change_password: true }),
    )
    render(<LocalAccountPanel onAuthenticated={onAuthenticated} />)
    fireEvent.change(screen.getByLabelText('Username or email'), {
      target: { value: 'rider' },
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'temporary-password' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.getByLabelText('New password')).toBeInTheDocument())
    vi.mocked(globalThis.fetch).mockResolvedValue(response({ must_change_password: false }))
    fireEvent.change(screen.getByLabelText('New password'), {
      target: { value: 'new-secure-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Password changed.'))
    expect(onAuthenticated).toHaveBeenCalledOnce()
  })

  it('exposes the optional sign-out callback', () => {
    const onSignedOut = vi.fn()
    render(<LocalAccountPanel onSignedOut={onSignedOut} />)
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(onSignedOut).toHaveBeenCalledOnce()
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

  it('uploads activities, shows batch outcomes, and deletes an imported activity', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(response({ batch_id: 'batch-1', status: 'queued' }, 202))
      .mockResolvedValueOnce(
        response({
          batch_id: 'batch-1',
          status: 'completed',
          total_files: 1,
          processed_files: 1,
          accepted_files: 1,
          duplicate_files: 0,
          failed_files: 0,
          files: [{ name: 'ride.gpx', status: 'accepted', activity_id: 'activity-1' }],
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    render(<LocalAccountPanel authenticated />)
    fireEvent.change(screen.getByLabelText('Activity files'), {
      target: { files: [new File(['<gpx />'], 'ride.gpx', { type: 'application/gpx+xml' })] },
    })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Upload activities' }))
    await waitFor(() =>
      expect(screen.getByText('completed: 1/1 files processed')).toBeInTheDocument(),
    )
    expect(screen.getByText('ride.gpx: accepted')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByText('ride.gpx: accepted')).not.toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining('/api/v1/game/account/activities/activity-1/'),
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})
