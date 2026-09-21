import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import OrgSettingsPage from './OrgSettingsPage'
import * as api from '../lib/api'
import * as orgCtx from '../context/OrgContext'
import { ChromeProvider } from '../context/ChromeContext'
import { atLeast, type Role } from '../lib/orgs'

// usePageChrome needs a ChromeProvider ancestor (it's the app shell's job in
// production, via App.tsx) — the page itself doesn't provide one.
function renderPage() {
  return render(<MemoryRouter><ChromeProvider><OrgSettingsPage /></ChromeProvider></MemoryRouter>)
}

function stubOrg(role: Role, orgCount = 2) {
  const org: api.Org = { id: 'org-1', name: 'Acme', personal: false, role }
  const orgs = [org, ...Array.from({ length: orgCount - 1 }, (_, i) => ({
    id: `other-${i}`, name: `Other ${i}`, personal: true, role: 'owner' as Role,
  }))]
  vi.spyOn(orgCtx, 'useOrg').mockReturnValue({
    orgId: org.id, org, orgs, role,
    can: (m: Role) => atLeast(role, m),
    orgPath: (to: string) => `/o/org-1${to}`,
    reloadOrgs: async () => {},
  } as never)
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('OrgSettingsPage', () => {
  it('lets an admin rename the organisation', async () => {
    stubOrg('admin')
    const rename = vi.spyOn(api, 'renameOrg').mockResolvedValue({
      id: 'org-1', name: 'Acme Ltd', personal: false, role: 'admin',
    })
    renderPage()

    const field = screen.getByLabelText('Organisation name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Acme Ltd')
    await userEvent.click(screen.getByRole('button', { name: 'Save name' }))

    await waitFor(() => expect(rename).toHaveBeenCalledWith('org-1', 'Acme Ltd'))
  })

  it('does not let a member rename it', () => {
    stubOrg('member')
    renderPage()
    expect(screen.queryByRole('button', { name: 'Save name' })).not.toBeInTheDocument()
  })

  it('explains to an admin why they cannot delete it', () => {
    stubOrg('admin')
    renderPage()
    expect(screen.getByText(/Only an owner can delete/)).toBeInTheDocument()
  })

  it('tells an owner when this is their last organisation', () => {
    stubOrg('owner', 1)
    renderPage()
    expect(screen.getByText(/your only organisation/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete organisation' })).not.toBeInTheDocument()
  })

  it('surfaces the server refusal when the organisation is not empty', async () => {
    stubOrg('owner')
    vi.spyOn(api, 'deleteOrg').mockRejectedValue(new Error('Empty the organisation first'))
    renderPage()

    await userEvent.click(screen.getByRole('button', { name: 'Delete organisation' }))
    await userEvent.click(screen.getByRole('button', { name: 'Yes, delete it' }))
    await waitFor(() => expect(screen.getByText('Empty the organisation first')).toBeInTheDocument())
  })
})
