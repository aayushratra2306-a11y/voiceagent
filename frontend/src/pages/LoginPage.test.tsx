/**
 * 2026-09-13 — a way to see the password you are typing.
 *
 * Asked for while creating test accounts: the field was masked with no way
 * to check it, on the same form that had just refused a password for being
 * too short without saying so (see lib/errorMessage.test.ts). Masked input
 * plus an unreadable error is a form you can only fail at by guessing.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ login: vi.fn() }) }))

import LoginPage from './LoginPage'

// testing-library only unmounts between tests on its own when the runner
// exposes a global afterEach; this config does not (no `globals: true`), so
// without this every test sees every earlier test's page still mounted.
afterEach(cleanup)

function renderPage() {
  render(<MemoryRouter><LoginPage /></MemoryRouter>)
  const password = document.querySelector('input[autocomplete$="password"]') as HTMLInputElement
  return { password }
}

describe('the show-password toggle', () => {
  it('is masked to begin with', () => {
    const { password } = renderPage()
    expect(password.type).toBe('password')
  })

  it('reveals the characters when pressed, and hides them again', () => {
    const { password } = renderPage()

    fireEvent.click(screen.getByRole('button', { name: 'Show password' }))
    expect(password.type).toBe('text')

    fireEvent.click(screen.getByRole('button', { name: 'Hide password' }))
    expect(password.type).toBe('password')
  })

  it('does not submit the form', () => {
    // A <button> inside a <form> is type="submit" unless told otherwise, so a
    // careless toggle would try to sign in every time it was pressed.
    renderPage()
    const toggle = screen.getByRole('button', { name: 'Show password' })

    expect(toggle.getAttribute('type')).toBe('button')
  })

  it('tells a screen reader whether the password is showing', () => {
    renderPage()
    const toggle = screen.getByRole('button', { name: 'Show password' })

    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Hide password' }).getAttribute('aria-pressed')).toBe('true')
  })
})

describe('creating an account', () => {
  it('says what the password needs before anyone gets it wrong', () => {
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Sign up' }))

    expect(screen.getByText(/at least 12 characters/i)).toBeTruthy()
  })
})
