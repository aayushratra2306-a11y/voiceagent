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
  { user_id: 'u2', email: 'viewer@x.com', role: 'viewer', joined: '2026-02-01' },
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

describe('MembersPage', () => {
  it('lists members with their roles', async () => {
    stubOrg('viewer')
    stubAuth('viewer@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())
    expect(screen.getByText('viewer@x.com')).toBeInTheDocument()
  })

  it('hides the add form from a viewer', async () => {
    stubOrg('viewer')
    stubAuth('viewer@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())
    expect(screen.queryByLabelText('Email address')).not.toBeInTheDocument()
  })

  // Task 5.2 — POST /orgs/{id}/members now creates an invitation rather
  // than adding the member outright; the invite/revoke behaviour itself is
  // covered in MembersPage.invite.test.tsx. This just keeps the page's
  // pre-existing invitation call wired up to the form.
  it('lets an admin submit an invitation', async () => {
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue([])
    const invite = vi.spyOn(api, 'inviteMember').mockResolvedValue({
      status: 'invitation sent', invite_path: '/invite/tok', expires_at: '2026-03-08',
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())

    await userEvent.type(screen.getByLabelText('Email address'), 'new@x.com')
    await userEvent.click(screen.getByRole('button', { name: 'Create invitation' }))

    await waitFor(() => expect(invite).toHaveBeenCalledWith('org-1', 'new@x.com', 'member'))
  })

  it('does not offer an admin any control over an owner', async () => {
    stubOrg('admin')
    stubAuth('admin@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue(MEMBERS)
    vi.spyOn(api, 'listInvitations').mockResolvedValue([])
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Remove owner@x.com' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove viewer@x.com' })).toBeInTheDocument()
  })

  it('shows the server message when the last owner tries to leave', async () => {
    stubOrg('owner')
    stubAuth('owner@x.com')
    vi.spyOn(api, 'listMembers').mockResolvedValue([MEMBERS[0]])
    vi.spyOn(api, 'listInvitations').mockResolvedValue([])
    vi.spyOn(api, 'removeMember').mockRejectedValue(new Error('An organisation needs at least one owner'))
    renderPage()
    await waitFor(() => expect(screen.getByText('owner@x.com')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: 'Leave organisation' }))
    await waitFor(() =>
      expect(screen.getByText('An organisation needs at least one owner')).toBeInTheDocument())
  })
})
