import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { acceptInvitation, getInvitation, type InvitationPreview } from '../lib/api'
import { useAuth } from '../context/AuthContext'
import PageLoader from '../components/PageLoader'

/**
 * The page behind an invitation link — the only screen in this app a
 * logged-out stranger is meant to reach besides /login. See App.tsx for why
 * this route sits outside PrivateRoute and OrgProvider: the visitor may have
 * no account and no organisation, and OrgProvider firing GET /orgs for them
 * would either fail outright or (worse) bounce them away from the one page
 * they were sent here to see.
 *
 * The invited email address itself is never rendered anywhere on this page,
 * in any state — only used internally to decide whether "Accept" makes
 * sense to offer. Printing it next to the signed-in account's address would
 * turn the page into an oracle for comparing the two; leaving it out avoids
 * that question rather than trying to answer it safely.
 */

function sameEmail(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase()
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-[#070711] flex items-center justify-center p-4 relative overflow-hidden">
      <div className="absolute top-[-20%] left-[-10%] w-[500px] h-[500px] rounded-full bg-violet-700/20 blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-20%] right-[-10%] w-[500px] h-[500px] rounded-full bg-indigo-700/20 blur-[120px] pointer-events-none" />
      <div className="w-full max-w-sm relative z-10 bg-white/5 border border-white/10 rounded-2xl p-6 backdrop-blur-sm shadow-xl text-center">
        {children}
      </div>
    </div>
  )
}

export default function AcceptInvitePage() {
  const { token } = useParams<{ token: string }>()
  const { token: authToken, ready, email, logout } = useAuth()
  const navigate = useNavigate()

  const [invite, setInvite] = useState<InvitationPreview | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [accepting, setAccepting] = useState(false)
  const [acceptError, setAcceptError] = useState('')

  useEffect(() => {
    // No setLoadFailed here: the route is /invite/:token so this branch is
    // unreachable in practice, and setting state synchronously inside an
    // effect just forces a second render. A missing token is derived at
    // render time instead (see the loadFailed check below).
    if (!token) return
    let cancelled = false
    getInvitation(token)
      .then(inv => { if (!cancelled) setInvite(inv) })
      .catch(() => { if (!cancelled) setLoadFailed(true) })
    return () => { cancelled = true }
  }, [token])

  async function handleAccept() {
    if (!token) return
    setAccepting(true)
    setAcceptError('')
    try {
      const { org_id } = await acceptInvitation(token)
      navigate(`/o/${org_id}/dashboard`, { replace: true })
    } catch (e) {
      // Covers 403 (wrong address), 409 (already a member) and 404 — every
      // one of request()'s thrown errors is already safe to show verbatim,
      // see the comment on acceptInvitation in lib/api.ts.
      setAcceptError((e as Error).message)
      setAccepting(false)
    }
  }

  // Invalid, expired, revoked or already-used — the backend returns the
  // same 404 for all four and this page deliberately does not try to guess
  // which one it was.
  if (loadFailed || !token) {
    return (
      <Shell>
        <p className="text-sm text-slate-300">This invitation link is no longer valid.</p>
      </Shell>
    )
  }

  // ready gates on AuthContext's own on-load silent-refresh attempt (see
  // PrivateRoute in App.tsx for the same wait) — without it a signed-in
  // visitor would flicker through the signed-out state for a moment.
  if (!ready || !invite) return <PageLoader />

  const signedIn = !!authToken
  const wrongAccount = signedIn && !!email && !sameEmail(email, invite.email)

  return (
    <Shell>
      <p className="text-white font-medium text-sm mb-1">Join {invite.org_name}</p>
      <p className="text-slate-400 text-xs mb-5">
        Invited by {invite.invited_by_email} as {invite.role}.
      </p>

      {wrongAccount ? (
        <div className="space-y-3">
          <p className="text-sm text-slate-300">
            This invitation was sent to a different account than the one you're signed in with.
          </p>
          <button
            type="button"
            onClick={() => logout()}
            className="w-full bg-white/10 hover:bg-white/15 text-white text-sm rounded-xl py-2.5 transition-colors"
          >
            Sign out and use a different account
          </button>
        </div>
      ) : signedIn ? (
        <div className="space-y-3">
          {acceptError && (
            <p className="text-sm rounded-xl px-3.5 py-2.5 bg-red-500/10 text-red-400 border border-red-500/20">
              {acceptError}
            </p>
          )}
          <button
            type="button"
            onClick={() => void handleAccept()}
            disabled={accepting}
            className="w-full bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 disabled:opacity-50 text-white font-semibold rounded-xl py-2.5 text-sm transition-all shadow-lg shadow-violet-900/30"
          >
            {accepting ? 'Joining…' : 'Accept invitation'}
          </button>
          {acceptError && (
            // A way out. This page sits outside AppShell, so it has no nav
            // of its own: without this, a visitor whose accept keeps being
            // refused can only re-click a button that will never work. That
            // is reachable — sameEmail() here is a plain trim+lowercase
            // check while the server uses the users index's own
            // normalisation, so the two can disagree and the server, which
            // is the authority, wins.
            <button
              type="button"
              onClick={() => logout()}
              className="w-full bg-white/10 hover:bg-white/15 text-white text-sm rounded-xl py-2.5 transition-colors"
            >
              Sign out and use a different account
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          {/* `next` carries the way back here through the sign-in flow — a
              query param rather than router state or storage, because it
              has to survive a full page load (this link may open a fresh
              tab with nothing else warmed up), a copy-pasted URL, and a
              switch between the sign-in and sign-up forms on the same
              page. LoginPage reads it and honours it after a successful
              login instead of its usual "last used organisation" landing. */}
          <Link
            to={`/?next=${encodeURIComponent(`/invite/${token}`)}`}
            className="block w-full bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white font-semibold rounded-xl py-2.5 text-sm transition-all shadow-lg shadow-violet-900/30"
          >
            Sign in
          </Link>
          <Link
            to={`/?next=${encodeURIComponent(`/invite/${token}`)}&mode=register`}
            className="block w-full bg-white/10 hover:bg-white/15 text-white text-sm rounded-xl py-2.5 transition-colors"
          >
            Sign up
          </Link>
        </div>
      )}
    </Shell>
  )
}
