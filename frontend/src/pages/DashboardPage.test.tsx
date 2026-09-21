import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import DashboardPage from './DashboardPage'
import * as api from '../lib/api'
import * as orgCtx from '../context/OrgContext'

const org: api.Org = { id: 'org-7', name: 'Acme', personal: false, role: 'admin' }

function useOrgStub() {
  return {
    orgId: org.id, org, orgs: [org], role: org.role,
    can: () => true,
    orgPath: (to: string) => `/o/${org.id}${to.startsWith('/') ? to : `/${to}`}`,
    reloadOrgs: async () => {},
  }
}

const bot: api.Bot = {
  id: 'bot-1', name: 'Nitya', system_prompt: '', voice_id: 'v', llm_model: 'm',
  language: 'en', timezone: 'UTC', booking_open: '09:00', booking_close: '18:00',
  slot_minutes: 30,
}

// testing-library only unmounts between tests on its own when the runner
// exposes a global afterEach; this config does not (no `globals: true`), so
// without this every test sees every earlier test's page still mounted (see
// LoginPage.test.tsx for the same note).
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('DashboardPage links', () => {
  it('opens a bot under the current organisation', async () => {
    vi.spyOn(orgCtx, 'useOrg').mockImplementation(useOrgStub as never)
    vi.spyOn(api, 'listBots').mockResolvedValue([bot])

    render(
      <MemoryRouter initialEntries={['/o/org-7/dashboard']}>
        <Routes>
          <Route path="/o/:orgId/dashboard" element={<DashboardPage />} />
          <Route path="/o/:orgId/bots/:id" element={<div data-testid="bot-page">bot</div>} />
        </Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('Nitya')).toBeInTheDocument())

    // The bot's name itself carries no click handler — the row's Settings
    // button (a plain gear icon, title="Settings") is what opens the bot,
    // so the row is found by its name text and the button queried within it.
    const row = screen.getByText('Nitya').closest('div.group') as HTMLElement
    fireEvent.click(within(row).getByTitle('Settings'))

    await waitFor(() => expect(screen.getByTestId('bot-page')).toBeInTheDocument())
  })
})

describe('DashboardPage role gating', () => {
  it('offers no New bot button to a viewer', async () => {
    vi.spyOn(orgCtx, 'useOrg').mockReturnValue({
      ...useOrgStub(), role: 'viewer', can: (m: string) => m === 'viewer',
    } as never)
    vi.spyOn(api, 'listBots').mockResolvedValue([])

    render(
      <MemoryRouter initialEntries={['/o/org-7/dashboard']}>
        <Routes><Route path="/o/:orgId/dashboard" element={<DashboardPage />} /></Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(api.listBots).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /new bot/i })).not.toBeInTheDocument()
  })

  it('still offers it to a member', async () => {
    vi.spyOn(orgCtx, 'useOrg').mockReturnValue({
      ...useOrgStub(), role: 'member', can: (m: string) => m !== 'admin' && m !== 'owner',
    } as never)
    vi.spyOn(api, 'listBots').mockResolvedValue([])

    render(
      <MemoryRouter initialEntries={['/o/org-7/dashboard']}>
        <Routes><Route path="/o/:orgId/dashboard" element={<DashboardPage />} /></Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByRole('button', { name: /new bot/i })).toBeInTheDocument())
  })
})

describe('DashboardPage error routing', () => {
  it('sends a failed bot list to the chooser rather than the login page', async () => {
    vi.spyOn(orgCtx, 'useOrg').mockImplementation(useOrgStub as never)
    vi.spyOn(api, 'listBots').mockRejectedValue(new Error('not a member of this organisation'))

    render(
      <MemoryRouter initialEntries={['/o/org-7/dashboard']}>
        <Routes>
          <Route path="/o/:orgId/dashboard" element={<DashboardPage />} />
          <Route path="/o" element={<div>chooser</div>} />
        </Routes>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('chooser')).toBeInTheDocument())
  })
})
