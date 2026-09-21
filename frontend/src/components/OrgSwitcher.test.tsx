import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import OrgSwitcher from './OrgSwitcher'
import * as orgCtx from '../context/OrgContext'
import type { Org } from '../lib/api'

// This config does not enable vitest's `globals` option (see
// OrgContext.test.tsx), so cleanup between tests must be requested
// explicitly or a later test's queries can match an earlier test's DOM.
const ORGS: Org[] = [
  { id: 'org-1', name: 'Personal', personal: true, role: 'owner' },
  { id: 'org-2', name: 'Acme', personal: false, role: 'viewer' },
]

function stub(current: Org) {
  vi.spyOn(orgCtx, 'useOrg').mockReturnValue({
    orgId: current.id, org: current, orgs: ORGS, role: current.role,
    can: () => true,
    orgPath: (to: string) => `/o/${current.id}${to}`,
    reloadOrgs: async () => {},
  } as never)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('OrgSwitcher', () => {
  it('shows the current organisation', () => {
    stub(ORGS[1])
    render(<MemoryRouter><OrgSwitcher /></MemoryRouter>)
    expect(screen.getByRole('button', { name: /Acme/ })).toBeInTheDocument()
  })

  it('lists my organisations with my role in each', async () => {
    stub(ORGS[0])
    render(<MemoryRouter><OrgSwitcher /></MemoryRouter>)
    await userEvent.click(screen.getByRole('button', { name: /Personal/ }))
    expect(screen.getByRole('menuitem', { name: /Acme/ })).toHaveTextContent('viewer')
    expect(screen.getByRole('menuitem', { name: /Create organisation/ })).toBeInTheDocument()
  })
})
