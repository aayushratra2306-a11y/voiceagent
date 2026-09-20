import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import RedirectToOrg from './components/RedirectToOrg'
import * as api from '../src/lib/api'

// See src/context/OrgContext.test.tsx for why this is needed: no vitest
// `globals`, so nothing unmounts the previous test's render on its own.
afterEach(cleanup)

const ORGS: api.Org[] = [
  { id: 'org-1', name: 'Personal', personal: true, role: 'owner' },
  { id: 'org-2', name: 'Acme', personal: false, role: 'admin' },
]

function renderOldPath(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/dashboard" element={<RedirectToOrg />} />
        <Route path="/bots/:id/tools" element={<RedirectToOrg />} />
        <Route path="/o/:orgId/*" element={<Landed />} />
      </Routes>
    </MemoryRouter>,
  )
}

function Landed() {
  return <div data-testid="landed">{window.location.pathname}</div>
}

describe('old addresses', () => {
  beforeEach(() => { vi.spyOn(api, 'listOrgs').mockResolvedValue(ORGS) })
  afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

  it('go to the same page under the last-used organisation', async () => {
    localStorage.setItem('voix:last-org', 'org-2')
    renderOldPath('/dashboard')
    await waitFor(() => expect(screen.getByTestId('landed')).toBeInTheDocument())
  })

  it('fall back to the personal organisation when there is no last-used one', async () => {
    renderOldPath('/bots/abc/tools')
    await waitFor(() => expect(screen.getByTestId('landed')).toBeInTheDocument())
  })
})
