import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import SessionPage from './SessionPage'
import * as api from '../lib/api'
import * as orgCtx from '../context/OrgContext'
import * as callCtx from '../context/CallContext'
import { ChromeProvider } from '../context/ChromeContext'

const bot: api.Bot = {
  id: 'bot-a-1', name: 'Org A Bot', system_prompt: '', voice_id: 'v', llm_model: 'm',
  language: 'en', timezone: 'UTC', booking_open: '09:00', booking_close: '18:00',
  slot_minutes: 30,
}

function stubOrg(orgId: string) {
  vi.spyOn(orgCtx, 'useOrg').mockReturnValue({
    orgId, org: { id: orgId, name: orgId, personal: false, role: 'member' }, orgs: [],
    role: 'member', can: () => true,
    orgPath: (to: string) => `/o/${orgId}${to.startsWith('/') ? to : `/${to}`}`,
    reloadOrgs: async () => {},
  } as never)
}

// Always mounted alongside <Routes>, outside anything the router matches on,
// so it observes the address SessionPage actually navigated to without
// itself being part of the route the click might remount.
function LocationProbe() {
  const { pathname } = useLocation()
  return <div data-testid="location">{pathname}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ChromeProvider>
        <LocationProbe />
        <Routes>
          <Route path="/o/:orgId/session/:id" element={<SessionPage />} />
        </Routes>
      </ChromeProvider>
    </MemoryRouter>,
  )
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('SessionPage — "Go to that call"', () => {
  // Whole-branch review finding 2 (2026-09-21): this button used to build
  // its target with orgPath(), which is the AMBIENT organisation (wherever
  // the user is currently looking), not the organisation the live call
  // actually belongs to. With a call live in org-a and the user viewing
  // org-b's session page, that navigated to /o/org-b/session/<org-a-bot-id>
  // — a bot that does not exist under org-b.
  it("goes to the call's own organisation, not the ambient one", async () => {
    // Ambient: org-b. The live call: org-a's bot.
    stubOrg('org-b')
    vi.spyOn(callCtx, 'useCall').mockReturnValue({
      bot, status: 'connected', speaking: false, muted: false,
      log: [], sources: null, connectedAt: Date.now(), openingDoc: null,
      callOrgId: 'org-a',
      startCall: vi.fn(), endCall: vi.fn(), toggleMute: vi.fn(), openSource: vi.fn(),
    } as never)
    // The address's own bot (org-b's bot-b-1) — distinct from the bot on
    // the live call, so isThisCall is false and "Go to that call" shows.
    vi.spyOn(api, 'listBots').mockResolvedValue([])

    renderAt('/o/org-b/session/bot-b-1')

    const goToCall = await screen.findByRole('button', { name: 'Go to that call' })
    fireEvent.click(goToCall)

    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/o/org-a/session/bot-a-1'))
  })
})
