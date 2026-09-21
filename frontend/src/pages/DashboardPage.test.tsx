import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
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

afterEach(() => vi.restoreAllMocks())

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
