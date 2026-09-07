import { useEffect, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { getPendingApprovalCount } from '../lib/api'
import { APPROVALS_CHANGED } from '../lib/events'
import { useAuth } from '../context/AuthContext'
import { useCall } from '../context/CallContext'
import { useChrome } from '../context/ChromeContext'
import CallBar, { useCallBarVisible } from './CallBar'

// How often the header re-checks for waiting approvals. An approval is
// raised mid-call and a person is expected to act on it while the caller is
// still on the line, so a badge that only appears on a page reload would
// miss the entire window it exists for. Twelve seconds is frequent enough
// to feel immediate at conversation pace, and slow enough that an idle
// dashboard left open all day costs a handful of requests an hour.
const APPROVAL_POLL_MS = 12_000

/**
 * The layer that was missing.
 *
 * Every page used to rebuild its own background, header and back button:
 * six copies of the header, five byte-identical copies of the background
 * blobs, six hardcoded back destinations. Three consequences followed from
 * that one gap, and all three are fixed here rather than one at a time.
 *
 *  1. Only the dashboard carried navigation, so Approvals and Webhooks were
 *     reachable from exactly one screen — and the approvals badge could
 *     therefore only ever appear in one place. It is now in the header of
 *     every page.
 *  2. Back buttons went to a fixed destination rather than back, so
 *     arriving somewhere from a call and pressing back landed on the
 *     dashboard instead of returning where you came from.
 *  3. Nothing sat between the router and the pages, so nothing could
 *     survive a navigation — which is why a live call could not.
 */
export default function AppShell() {
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const { logout } = useAuth()
  const { endCall } = useCall()
  const { chrome } = useChrome()
  const navigate = useNavigate()
  const location = useLocation()
  const barVisible = useCallBarVisible()

  useEffect(() => {
    let cancelled = false
    // Failures are swallowed deliberately: this is an ambient indicator, and
    // one flaky poll must not surface an error over a page the user is
    // using for something else. A missed tick simply shows the previous
    // count until the next one lands.
    const check = () =>
      getPendingApprovalCount()
        .then(n => { if (!cancelled) setPendingApprovals(n) })
        .catch(() => {})

    check()
    const timer = setInterval(check, APPROVAL_POLL_MS)
    // A decision made on the Approvals page has to clear the badge NOW.
    // Waiting out the poll would leave the count you just acted on sitting
    // in the header, which reads as if the click did nothing.
    window.addEventListener(APPROVALS_CHANGED, check)
    return () => {
      cancelled = true
      clearInterval(timer)
      window.removeEventListener(APPROVALS_CHANGED, check)
    }
  }, [])

  // Also re-check on every navigation. The event above covers decisions
  // made in this tab; this covers arriving back from anywhere with a count
  // that moved while the page was in the background.
  useEffect(() => {
    getPendingApprovalCount().then(setPendingApprovals).catch(() => {})
  }, [location.pathname])

  function goBack() {
    // `key` is 'default' only for the first entry the app rendered — a
    // bookmark, a pasted link, a fresh tab. Anywhere else there is real
    // history to step back through, and stepping back is what the arrow
    // should do: it returns you where you actually came from, including to
    // a call in progress.
    if (location.key !== 'default') navigate(-1)
    else navigate(chrome.backTo ?? '/dashboard')
  }

  function signOut() {
    // A signed-out session has no business holding a microphone open.
    endCall()
    logout()
    navigate('/')
  }

  const navItem = 'text-xs transition-colors px-3 py-1.5 rounded-lg hover:bg-white/5'

  return (
    <div className="min-h-screen bg-[#070711] text-white relative overflow-hidden">
      {chrome.blobs !== false && (
        <>
          <div className="absolute top-[-15%] right-[-10%] w-[500px] h-[500px] rounded-full bg-violet-700/15 blur-[130px] pointer-events-none" />
          <div className="absolute bottom-[-20%] left-[-5%] w-[400px] h-[400px] rounded-full bg-indigo-700/15 blur-[120px] pointer-events-none" />
        </>
      )}

      <header className="relative z-10 border-b border-white/8 px-6 py-4 flex items-center justify-between gap-3 backdrop-blur-sm">
        <div className="flex items-center gap-2.5 min-w-0">
          {chrome.title && (
            <button
              onClick={goBack}
              aria-label="Back"
              className="p-1.5 text-slate-500 hover:text-white hover:bg-white/8 rounded-lg transition-all shrink-0"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
              </svg>
            </button>
          )}

          <button
            onClick={() => navigate('/dashboard')}
            className="flex items-center gap-2 shrink-0"
            aria-label="Auris home"
          >
            {/* Logomark */}
            <svg width="18" height="18" viewBox="0 0 26 26" fill="none">
              <rect x="0"  y="14" width="5" height="12" rx="2.5" fill="#00D4FF" opacity="0.5"/>
              <rect x="7"  y="7"  width="5" height="19" rx="2.5" fill="#00D4FF" opacity="0.75"/>
              <rect x="14" y="2"  width="5" height="24" rx="2.5" fill="#00D4FF"/>
              <rect x="21" y="9"  width="5" height="17" rx="2.5" fill="#00D4FF" opacity="0.6"/>
            </svg>
            <span style={{ fontFamily: "'Barlow Condensed', sans-serif", fontWeight: 900, fontSize: '1.45rem', letterSpacing: '0.07em', color: '#00D4FF', lineHeight: 1, textShadow: '0 0 16px rgba(0,212,255,0.22)' }}>
              AURIS
            </span>
          </button>

          {chrome.title && (
            <>
              <span className="text-slate-700 shrink-0" aria-hidden="true">/</span>
              <h1 className="text-sm font-semibold text-slate-200 truncate">{chrome.title}</h1>
            </>
          )}
        </div>

        <nav className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => navigate('/approvals')}
            // aria-label carries the count too: the badge is a visual cue,
            // and a screen reader announcing a bare "Approvals" would lose
            // the only part that says something needs doing.
            aria-label={
              pendingApprovals > 0
                ? `Approvals, ${pendingApprovals} waiting`
                : 'Approvals'
            }
            className={`relative ${navItem} ${
              pendingApprovals > 0
                ? 'text-amber-300 hover:text-amber-200'
                : 'text-slate-500 hover:text-slate-300'
            }`}
          >
            Approvals
            {pendingApprovals > 0 && (
              <span
                // Not a red dot: red reads as "something is broken", and a
                // waiting approval is a normal request for a decision.
                // Amber says "your turn" without implying a failure.
                className="ml-1.5 inline-flex items-center justify-center min-w-[17px] h-[17px] px-1 rounded-full bg-amber-400 text-[10px] font-bold text-neutral-900 align-middle tabular-nums"
              >
                {/* Capped so a long-neglected queue can't stretch the nav */}
                {pendingApprovals > 99 ? '99+' : pendingApprovals}
              </span>
            )}
          </button>
          <button
            onClick={() => navigate('/webhooks')}
            className={`${navItem} text-slate-500 hover:text-slate-300`}
          >
            Webhooks
          </button>
          <button onClick={signOut} className={`${navItem} text-slate-500 hover:text-slate-300`}>
            Sign out
          </button>
        </nav>
      </header>

      {/* Reserve the bar's height so it never covers the last row of a page
          — a Deny button hidden behind a floating bar is a button that
          doesn't exist. */}
      <div className={barVisible ? 'pb-28' : ''}>
        <Outlet />
      </div>

      <CallBar />
    </div>
  )
}
