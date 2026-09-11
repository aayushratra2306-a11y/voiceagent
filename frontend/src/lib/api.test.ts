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
