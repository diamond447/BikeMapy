import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from './App'

describe('BikeMapy shell', () => {
  it('introduces the route catalogue and exposes map landmarks', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: /find the ride/i })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /route map/i })).toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: /search routes/i })).toBeInTheDocument()
  })
})
