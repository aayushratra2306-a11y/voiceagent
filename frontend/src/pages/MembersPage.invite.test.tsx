import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import MembersPage from './MembersPage'
import * as api from '../lib/api'
import * as orgCtx from '../context/OrgContext'
import * as authCtx from '../context/AuthContext'
import { ChromeProvider } from '../context/ChromeContext'
import type { Org } from '../lib/api'
import { atLeast, type Role } from '../lib/orgs'

// usePageChrome needs a ChromeProvider ancestor (it's the app shell's job in
// production, via App.tsx) — the page itself doesn't provide one.
function renderPage() {
  return render(<MemoryRouter><ChromeProvider><MembersPage /></ChromeProvider></MemoryRouter>)
}

const MEMBERS: api.Member[] = [
  { user_id: 'u1', email: 'owner@x.com', role: 'owner', joined: '2026-01-01' },
]

const PENDING: api.Invitation[] = [
  { id: 'inv-1', email: 'pending@x.com', role: 'member', invited_at: '2026-09-20T00:00:00Z', expires_at: '2026-09-27T00:00:00Z' },
]

function stubOrg(role: Role) {
  const org: Org = { id: 'org-1', name: 'Acme', personal: false, role }
  vi.spyOn(orgCtx, 'useOrg').mockReturnValue({
    orgId: org.id, org, orgs: [org], role,
    can: (m: Role) => atLeast(role, m),
    orgPath: (to: string) => `/o/org-1${to}`,
    reloadOrgs: async () => {},
  } as never)
}

function stubAuth(email: string | null) {
  vi.spyOn(authCtx, 'useAuth').mockReturnValue({
    token: 'x', ready: true, email, login: () => {}, logout: () => {},
  } as never)
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('MembersPage — invitations', () => {
  it('lets an admin create an invitation and shows the absolute link, not the bare path', async () => {
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue([])
    const invite = vi.spyOn(api, 'inviteMember').mockResolvedValue({
      status: 'invitation sent',
      invite_path: '/invite/tok_abc123',
      expires_at: '2026-09-30T12:00:00Z',
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())

    await userEvent.type(screen.getByLabelText('Email address'), 'new@x.com')
    await userEvent.click(screen.getByRole('button', { name: 'Create invitation' }))

    await waitFor(() => expect(invite).toHaveBeenCalledWith('org-1', 'new@x.com', 'member'))

    const linkField = await screen.findByLabelText('Invitation link')
    const expected = window.location.origin + '/invite/tok_abc123'
    expect(linkField).toHaveValue(expected)
    // The bare path alone must not be what's shown as the link.
    expect(linkField).not.toHaveValue('/invite/tok_abc123')
  })

  it('does not claim the person was added or notified', async () => {
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue([])
    vi.spyOn(api, 'inviteMember').mockResolvedValue({
      status: 'invitation sent',
      invite_path: '/invite/tok_abc123',
      expires_at: '2026-09-30T12:00:00Z',
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())

    await userEvent.type(screen.getByLabelText('Email address'), 'new@x.com')
    await userEvent.click(screen.getByRole('button', { name: 'Create invitation' }))

    await screen.findByLabelText('Invitation link')
    expect(screen.queryByText(/user added/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/we've emailed/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/emailed them/i)).not.toBeInTheDocument()
    // Wording must make clear the admin sends it themselves.
    expect(screen.getByText(/send (it|this link) to them yourself/i)).toBeInTheDocument()
  })

  it('renders the pending invitations list for an admin', async () => {
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue(PENDING)
    renderPage()
    await waitFor(() => expect(screen.getByText('pending@x.com')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /revoke/i })).toBeInTheDocument()
  })

  it('removes a row when revoke succeeds', async () => {
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    const listInv = vi.spyOn(api, 'listInvitations')
      .mockResolvedValueOnce(PENDING)
      .mockResolvedValueOnce([])
    const revoke = vi.spyOn(api, 'revokeInvitation').mockResolvedValue(undefined)
    renderPage()
    await waitFor(() => expect(screen.getByText('pending@x.com')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: /revoke/i }))

    await waitFor(() => expect(revoke).toHaveBeenCalledWith('org-1', 'inv-1'))
    await waitFor(() => expect(screen.queryByText('pending@x.com')).not.toBeInTheDocument())
    expect(listInv).toHaveBeenCalledTimes(2)
  })

  it('surfaces the error when revoke fails, and leaves the row in place', async () => {
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue(PENDING)
    vi.spyOn(api, 'revokeInvitation').mockRejectedValue(new Error('Only an owner can change another owner'))
    renderPage()
    await waitFor(() => expect(screen.getByText('pending@x.com')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: /revoke/i }))

    await waitFor(() =>
      expect(screen.getByText('Only an owner can change another owner')).toBeInTheDocument())
    // The row must still be there — a failed revoke did nothing server-side.
    expect(screen.getByText('pending@x.com')).toBeInTheDocument()
  })

  it('shows a member no invite form and no revoke control, and never fetches invitations', async () => {
    stubOrg('member')
    stubAuth('member@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    const listInv = vi.spyOn(api, 'listInvitations').mockResolvedValue(PENDING)
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())

    expect(screen.queryByLabelText('Email address')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create invitation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /revoke/i })).not.toBeInTheDocument()
    expect(listInv).not.toHaveBeenCalled()
  })

  it('shows the expiry date alongside the invitation link', async () => {
    stubOrg('owner')
    stubAuth('owner@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue([])
    vi.spyOn(api, 'inviteMember').mockResolvedValue({
      status: 'invitation sent',
      invite_path: '/invite/tok_xyz',
      expires_at: '2026-09-30T12:00:00Z',
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())

    await userEvent.type(screen.getByLabelText('Email address'), 'new@x.com')
    await userEvent.click(screen.getByRole('button', { name: 'Create invitation' }))

    await screen.findByLabelText('Invitation link')
    expect(screen.getByText(/expires/i)).toBeInTheDocument()
  })

  // --- review round 1 ---------------------------------------------------------

  it('does not offer an admin a revoke control on an owner invitation', async () => {
    // Mirrors "does not offer an admin any control over an owner" in
    // MembersPage.test.tsx. The server guards this (403 "Only an owner can
    // change another owner"), so a button here would always fail.
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue([
      { id: 'inv-o', email: 'heir@x.com', role: 'owner', invited_at: '2026-09-20T00:00:00Z', expires_at: '2026-09-27T00:00:00Z' },
    ])
    renderPage()

    await waitFor(() => expect(screen.getByText('heir@x.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Revoke invitation to heir@x.com' })).not.toBeInTheDocument()
  })

  it('does offer an owner a revoke control on an owner invitation', async () => {
    stubOrg('owner')
    stubAuth('owner@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue([
      { id: 'inv-o', email: 'heir@x.com', role: 'owner', invited_at: '2026-09-20T00:00:00Z', expires_at: '2026-09-27T00:00:00Z' },
    ])
    renderPage()

    await waitFor(() => expect(screen.getByText('heir@x.com')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Revoke invitation to heir@x.com' })).toBeInTheDocument()
  })
})
