import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { OrgProvider, useOrg } from './OrgContext'
import * as api from '../lib/api'

// This project does not enable vitest's `globals` option, so — like every
// other test file here (see pages/LoginPage.test.tsx) — testing-library's
// automatic cleanup-between-tests never fires unless asked for explicitly.
// Without it, org-2's probe from one test is still mounted when the next
// test renders its own, and both match the same query.
afterEach(cleanup)

const ORGS: api.Org[] = [
  { id: 'org-1', name: 'Personal', personal: true, role: 'owner' },
  { id: 'org-2', name: 'Acme', personal: false, role: 'viewer' },
]

function Probe() {
  const { orgId, org, role, can, orgPath } = useOrg()
  return (
    <div>
      <span data-testid="id">{orgId}</span>
      <span data-testid="name">{org.name}</span>
      <span data-testid="role">{role}</span>
      <span data-testid="can-member">{String(can('member'))}</span>
      <span data-testid="path">{orgPath('/bots/new')}</span>
    </div>
  )
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/o/:orgId" element={<OrgProvider />}>
          <Route path="probe" element={<Probe />} />
        </Route>
        <Route path="/o" element={<div>chooser</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('OrgProvider', () => {
  beforeEach(() => { vi.spyOn(api, 'listOrgs').mockResolvedValue(ORGS) })
  afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

  it('takes the organisation from the address and exposes my role in it', async () => {
    renderAt('/o/org-2/probe')
    await waitFor(() => expect(screen.getByTestId('name')).toHaveTextContent('Acme'))
    expect(screen.getByTestId('id')).toHaveTextContent('org-2')
    expect(screen.getByTestId('role')).toHaveTextContent('viewer')
    expect(screen.getByTestId('can-member')).toHaveTextContent('false')
  })

  it('hands the organisation to the API client', async () => {
    const setActive = vi.spyOn(api, 'setActiveOrg')
    renderAt('/o/org-1/probe')
    await waitFor(() => expect(screen.getByTestId('name')).toHaveTextContent('Personal'))
    expect(setActive).toHaveBeenCalledWith('org-1')
  })

  it('builds paths under the current organisation', async () => {
    renderAt('/o/org-1/probe')
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/o/org-1/bots/new'))
  })

  it('sends an organisation I am not a member of to the chooser', async () => {
    renderAt('/o/org-999/probe')
    await waitFor(() => expect(screen.getByText('chooser')).toBeInTheDocument())
  })

  it('remembers the organisation for next time', async () => {
    renderAt('/o/org-2/probe')
    await waitFor(() => expect(screen.getByTestId('name')).toHaveTextContent('Acme'))
    expect(localStorage.getItem('voix:last-org')).toBe('org-2')
  })
})
