import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import AcceptInvitePage from './AcceptInvitePage'
import App from '../App'
import * as api from '../lib/api'
import * as authCtx from '../context/AuthContext'

// This project does not enable vitest's `globals` option, so — like every
// other test file here (see LoginPage.test.tsx) — testing-library's
// automatic cleanup-between-tests never fires unless asked for explicitly.
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })

const PREVIEW: api.InvitationPreview = {
  org_name: 'Acme',
  role: 'member',
  email: 'invitee@x.com',
  invited_by_email: 'admin@x.com',
}

function stubAuth(token: string | null, email: string | null, logout: () => void = () => {}) {
  vi.spyOn(authCtx, 'useAuth').mockReturnValue({
    token, ready: true, email, login: () => {}, logout,
  } as never)
}

function Landed() {
  // useLocation(), not window.location.pathname — see App.redirects.test.tsx
  // for why: under MemoryRouter, window.location stays constant.
  const { pathname } = useLocation()
  return <div data-testid="landed">{pathname}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/invite/:token" element={<AcceptInvitePage />} />
        <Route path="/o/:orgId/dashboard" element={<Landed />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('signed in with a matching email', () => {
  it('shows the org, role and inviter, and lands on the dashboard after accepting', async () => {
    stubAuth('tok', 'invitee@x.com')
    vi.spyOn(api, 'getInvitation').mockResolvedValue(PREVIEW)
    const accept = vi.spyOn(api, 'acceptInvitation').mockResolvedValue({ org_id: 'org-9', role: 'member' })
    renderAt('/invite/tok-abc')

    await waitFor(() => expect(screen.getByText('Acme', { exact: false })).toBeInTheDocument())
    expect(screen.getByText(/admin@x.com/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /accept/i }))

    await waitFor(() => expect(accept).toHaveBeenCalledWith('tok-abc'))
    await waitFor(() => expect(screen.getByTestId('landed')).toHaveTextContent('/o/org-9/dashboard'))
  })

  it('surfaces a failed accept without navigating away', async () => {
    stubAuth('tok', 'invitee@x.com')
    vi.spyOn(api, 'getInvitation').mockResolvedValue(PREVIEW)
    vi.spyOn(api, 'acceptInvitation').mockRejectedValue(
      new Error("You're already a member of this organisation"),
    )
    renderAt('/invite/tok-abc')

    await waitFor(() => expect(screen.getByRole('button', { name: /accept/i })).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /accept/i }))

    await waitFor(() =>
      expect(screen.getByText("You're already a member of this organisation")).toBeInTheDocument())
    expect(screen.queryByTestId('landed')).not.toBeInTheDocument()
  })

  it('offers a way out when accepting keeps failing', async () => {
    // Review finding: this page sits outside AppShell, so it has no nav of
    // its own. Showing only an error plus a button that will never succeed
    // strands the visitor. Reachable on a 409 ("already a member") and on
    // any 404 — neither of which the page's own email check can predict,
    // because the server is the authority and this page only guesses at
    // which control to offer.
    stubAuth('tok', 'invitee@x.com')
    vi.spyOn(api, 'getInvitation').mockResolvedValue(PREVIEW)
    vi.spyOn(api, 'acceptInvitation').mockRejectedValue(
      new Error('This invitation was sent to a different email address'),
    )
    renderAt('/invite/tok-abc')

    await waitFor(() => expect(screen.getByRole('button', { name: /accept/i })).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /sign out/i })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /accept/i }))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /sign out/i })).toBeInTheDocument())
  })
})

describe('signed out', () => {
  it('shows the preview without any auth and links to sign in / sign up that return here', async () => {
    stubAuth(null, null)
    vi.spyOn(api, 'getInvitation').mockResolvedValue(PREVIEW)
    renderAt('/invite/tok-abc')

    await waitFor(() => expect(screen.getByText('Acme', { exact: false })).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /accept/i })).not.toBeInTheDocument()

    const signIn = screen.getByRole('link', { name: /sign in/i })
    const signInUrl = new URL(signIn.getAttribute('href')!, 'http://example.test')
    expect(signInUrl.pathname).toBe('/')
    expect(signInUrl.searchParams.get('next')).toBe('/invite/tok-abc')

    const signUp = screen.getByRole('link', { name: /sign up/i })
    const signUpUrl = new URL(signUp.getAttribute('href')!, 'http://example.test')
    expect(signUpUrl.searchParams.get('next')).toBe('/invite/tok-abc')
    expect(signUpUrl.searchParams.get('mode')).toBe('register')
  })
})

describe('an invalid, expired, revoked or already-used token', () => {
  it('shows one plain message and nothing that would distinguish the cause', async () => {
    stubAuth(null, null)
    vi.spyOn(api, 'getInvitation').mockRejectedValue(new Error('Invitation not found'))
    renderAt('/invite/bad-token')

    await waitFor(() => expect(screen.getByText(/no longer valid/i)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /accept/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /sign in/i })).not.toBeInTheDocument()
  })
})

describe('signed in as the wrong account', () => {
  it('names the invited address so the visitor knows which account to use, and offers a way out', async () => {
    // This reverses an earlier decision to hide the invited address. The
    // whole-branch review showed hiding it bought nothing — it is in the
    // getInvitation response and so in devtools regardless — while it cost
    // the commonest path: register with the wrong address, get told the
    // invitation is for a different account, and have no way to discover
    // which one. The signed-in address is still not printed beside it;
    // the visitor already knows that one.
    const logout = vi.fn()
    stubAuth('tok', 'someone-else@x.com', logout)
    vi.spyOn(api, 'getInvitation').mockResolvedValue(PREVIEW)
    renderAt('/invite/tok-abc')

    await waitFor(() => expect(screen.getAllByText(/different account/i).length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /^accept/i })).not.toBeInTheDocument()

    expect(screen.getAllByText(/invitee@x.com/).length).toBeGreaterThan(0)
    expect(screen.queryByText('someone-else@x.com')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /sign out/i }))
    expect(logout).toHaveBeenCalled()
  })
})

describe('the routing trap', () => {
  it('the real /invite/:token route renders for a signed-out visitor without OrgProvider firing GET /orgs or bouncing to login', async () => {
    vi.spyOn(api, 'trySilentRefresh').mockResolvedValue(null)
    const listOrgs = vi.spyOn(api, 'listOrgs')
    vi.spyOn(api, 'getInvitation').mockResolvedValue(PREVIEW)
    window.history.pushState({}, '', '/invite/tok-route')

    render(<App />)

    await waitFor(() => expect(screen.getByText('Acme', { exact: false })).toBeInTheDocument())
    expect(listOrgs).not.toHaveBeenCalled()
    expect(screen.queryByText('Sign in to your account')).not.toBeInTheDocument()
  })
})
