/**
 * Review finding I19 (2026-09-10) — signing out did not necessarily sign
 * you out.
 *
 * `refreshInFlight` is a module singleton, deliberately: several requests
 * 401-ing at once must share ONE /auth/refresh, because the refresh token
 * rotates server-side and only the first of a parallel burst could win.
 * That part is right and stays.
 *
 * What was missing is that nothing ever told it the session had ended. A
 * refresh in flight when the user pressed Sign out resolved a moment
 * later and ran `localStorage.setItem('token', …)` — putting a FRESH
 * access token into storage after sign-out. Combined with `/auth/logout`
 * being fire-and-forget with its errors swallowed, a slow or failed
 * server-side revocation left the refresh cookie valid too, so the next
 * app load silently resumed the session the user thought they had ended.
 * On a shared machine that is the whole problem.
 *
 * The shape of every test here is the same: start a refresh, hold its
 * response open, sign out, then let it land. Nothing it does afterwards
 * may reach localStorage.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { logout, trySilentRefresh } from './api'
import { setActiveOrg, listBots, uploadDocument, fetchDocumentBlobUrl, listOrgs, addMember, connectBot } from './api'

/** A promise whose settlement this test controls, to hold a fetch open. */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

function jsonResponse(body: unknown) {
  return { ok: true, json: async () => body } as unknown as Response
}

describe('signing out while a token refresh is in flight', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('does not let the refresh put a token back after logout', async () => {
    const pending = deferred<Response>()
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/auth/refresh')) return pending.promise
      return Promise.resolve(jsonResponse({}))   // the logout call itself
    })
    vi.stubGlobal('fetch', fetchMock)

    localStorage.setItem('token', 'the-old-one')

    // A refresh is under way — the user is mid-session and something 401'd.
    const refreshing = trySilentRefresh()

    // They press Sign out before it comes back.
    await logout()
    expect(localStorage.getItem('token')).toBeNull()

    // Now the refresh lands, carrying a perfectly valid new token.
    pending.resolve(jsonResponse({ access_token: 'a-fresh-one' }))
    await refreshing

    expect(localStorage.getItem('token')).toBeNull()
  })

  it('reports the refresh as failed rather than succeeding into the void', async () => {
    const pending = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/auth/refresh')) return pending.promise
      return Promise.resolve(jsonResponse({}))
    }))

    const refreshing = trySilentRefresh()
    await logout()
    pending.resolve(jsonResponse({ access_token: 'a-fresh-one' }))

    // trySilentRefresh resolves null on failure, which is what the caller
    // (AuthProvider, on app load) treats as "no session to resume". A
    // refresh that was overtaken by a sign-out has to look like that, not
    // like a live session.
    await expect(refreshing).resolves.toBeNull()
  })

  it('lets a new sign-in refresh normally afterwards', async () => {
    // Failing closed on a stale refresh must not poison the module for the
    // next session — the guard is per-refresh, not a one-way latch.
    const stale = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/auth/refresh')) return stale.promise
      return Promise.resolve(jsonResponse({}))
    }))

    const abandoned = trySilentRefresh()
    await logout()
    stale.resolve(jsonResponse({ access_token: 'stale' }))
    await abandoned

    // A fresh login, then a refresh that belongs to the new session.
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve(jsonResponse({ access_token: 'genuinely-new' })),
    ))

    await expect(trySilentRefresh()).resolves.toBe('genuinely-new')
    expect(localStorage.getItem('token')).toBe('genuinely-new')
  })

  it('clears the shared in-flight promise so the next caller starts a new refresh', async () => {
    // Without this, a caller arriving after sign-out would be handed the
    // abandoned promise and wait on a result that is guaranteed to be
    // discarded.
    const stale = deferred<Response>()
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/auth/refresh')) return stale.promise
      return Promise.resolve(jsonResponse({}))
    })
    vi.stubGlobal('fetch', fetchMock)

    void trySilentRefresh()
    const refreshCallsBefore = fetchMock.mock.calls
      .filter(c => String(c[0]).endsWith('/auth/refresh')).length
    await logout()

    void trySilentRefresh()
    const refreshCallsAfter = fetchMock.mock.calls
      .filter(c => String(c[0]).endsWith('/auth/refresh')).length

    expect(refreshCallsAfter).toBeGreaterThan(refreshCallsBefore)
  })
})

function mockFetch(impl: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const spy = vi.fn(impl)
  vi.stubGlobal('fetch', spy)
  return spy
}
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

function headerOf(spy: ReturnType<typeof mockFetch>, call = 0): Record<string, string> {
  const init = spy.mock.calls[call][1] as RequestInit
  return Object.fromEntries(new Headers(init.headers).entries())
}

describe('X-Org-Id', () => {
  beforeEach(() => { localStorage.setItem('token', 't'); setActiveOrg('org-1') })
  afterEach(() => { setActiveOrg(null); localStorage.clear(); vi.unstubAllGlobals() })

  it('rides on every ordinary request', async () => {
    const spy = mockFetch(() => json([]))
    await listBots()
    expect(headerOf(spy)['x-org-id']).toBe('org-1')
  })

  it('rides on a document upload, which builds its own headers', async () => {
    const spy = mockFetch(() => json({ id: 'd1', filename: 'a.pdf', chunk_count: 1, created_at: 'now' }))
    await uploadDocument('bot-1', new File(['x'], 'a.pdf'))
    expect(headerOf(spy)['x-org-id']).toBe('org-1')
  })

  it('rides on a blob fetch, which builds its own headers', async () => {
    const spy = mockFetch(() => new Response(new Blob(['x']), { status: 200 }))
    vi.stubGlobal('URL', { ...URL, createObjectURL: () => 'blob:x' })
    await fetchDocumentBlobUrl('doc-1')
    expect(headerOf(spy)['x-org-id']).toBe('org-1')
  })

  it('is left off /orgs, where the path or nothing decides the organisation', async () => {
    const spy = mockFetch(() => json([]))
    await listOrgs()
    expect(headerOf(spy)['x-org-id']).toBeUndefined()
  })

  it('is left off the members endpoints, whose path names the organisation', async () => {
    const spy = mockFetch(() => json({ user_id: 'u2', email: 'b@x.com', role: 'member', joined: 'now' }))
    await addMember('org-2', 'b@x.com', 'member')
    expect(headerOf(spy)['x-org-id']).toBeUndefined()
    expect(spy.mock.calls[0][0]).toBe('/orgs/org-2/members')
  })

  it('sends no header at all when no organisation is active', async () => {
    setActiveOrg(null)
    const spy = mockFetch(() => json([]))
    await listBots()
    expect(headerOf(spy)['x-org-id']).toBeUndefined()
  })

  // Task 5.1.7 — a live call passes the organisation it started in
  // explicitly, so it must win over whatever is active now, not just agree
  // with it. Reading the header-merge order in request()/orgHeaders() is
  // not proof by itself: these assert the header that actually lands on
  // the wire, with the override and the active organisation deliberately
  // set to two DIFFERENT values, so the test fails if the override stops
  // winning and the ambient one leaks through instead.
  it('lets fetchDocumentBlobUrl override the active organisation on its own request', async () => {
    const spy = mockFetch(() => new Response(new Blob(['x']), { status: 200 }))
    vi.stubGlobal('URL', { ...URL, createObjectURL: () => 'blob:x' })
    setActiveOrg('org-2')
    await fetchDocumentBlobUrl('doc-1', 'org-1')
    expect(headerOf(spy)['x-org-id']).toBe('org-1')
  })

  it('lets connectBot override the active organisation on its own request', async () => {
    const spy = mockFetch(() => json({ sdp: 'v=0', type: 'answer', pc_id: 'pc-1' }))
    setActiveOrg('org-2')
    await connectBot('bot-1', 'v=0', 'offer', 'org-1')
    expect(headerOf(spy)['x-org-id']).toBe('org-1')
  })
})
