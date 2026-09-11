import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useCall, isCallActive } from '../context/CallContext'

/** mm:ss, and hh:mm:ss once a call runs past the hour. */
function formatDuration(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000))
  const s = String(total % 60).padStart(2, '0')
  const m = String(Math.floor(total / 60) % 60).padStart(2, '0')
  const h = Math.floor(total / 3600)
  return h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`
}

/**
 * Whether the bar is on screen right now. The shell needs to know too, so it
 * can reserve the space instead of letting the bar cover the last row of
 * whatever page is underneath.
 */
export function useCallBarVisible() {
  const { bot, status } = useCall()
  const { pathname } = useLocation()
  if (!isCallActive(status) || !bot) return false
  // Already looking at this call — the bar would just be a smaller copy.
  return pathname !== `/session/${bot.id}`
}

/**
 * The proof that the call outlives the page. Pinned to the bottom of every
 * screen while a call is up, so someone can walk to Approvals, make a
 * decision and come back — with the caller still on the line and a visible
 * reminder that they are.
 *
 * Hidden on the call's own page, where all of this is on screen already at
 * full size.
 */
export default function CallBar() {
  const { bot, status, speaking, muted, connectedAt, endCall, toggleMute } = useCall()
  const navigate = useNavigate()
  const [now, setNow] = useState(() => Date.now())

  const active = isCallActive(status)
  const visible = useCallBarVisible()

  useEffect(() => {
    if (!active || connectedAt === null) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [active, connectedAt])

  if (!visible || !bot) return null

  return (
    <div className="fixed bottom-0 inset-x-0 z-50 px-4 pb-4 pointer-events-none">
      <div
        role="status"
        aria-live="polite"
        className="pointer-events-auto mx-auto max-w-2xl flex items-center gap-3 rounded-2xl border border-white/10 bg-[#0e0e1c]/95 backdrop-blur-md px-4 py-3 shadow-2xl shadow-black/60"
      >
        {/* Live indicator — pulses green while the caller is actually
            talking, so the bar reflects the room, not just a boolean. */}
        <span className="relative flex h-2.5 w-2.5 shrink-0">
          {status === 'connected' && (
            <span className={`absolute inline-flex h-full w-full rounded-full opacity-70 animate-ping ${
              speaking ? 'bg-emerald-400' : 'bg-violet-400'
            }`} />
          )}
          <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${
            status === 'connecting' ? 'bg-amber-400'
            : speaking ? 'bg-emerald-400' : 'bg-violet-400'
          }`} />
        </span>

        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-white truncate">{bot.name}</p>
          <p className="text-[11px] text-slate-500">
            {status === 'connecting'
              ? 'Connecting…'
              : muted
              ? 'On call · microphone muted'
              : 'On call'}
          </p>
        </div>

        {connectedAt !== null && (
          <span className="text-xs text-slate-400 tabular-nums shrink-0">
            {formatDuration(now - connectedAt)}
          </span>
        )}

        <button
          onClick={toggleMute}
          disabled={status !== 'connected'}
          aria-pressed={muted}
          title={muted ? 'Unmute microphone' : 'Mute microphone'}
          className={`p-2 rounded-xl transition-all shrink-0 disabled:opacity-40 ${
            muted
              ? 'bg-red-500/15 text-red-300 hover:bg-red-500/25'
              : 'text-slate-400 hover:text-white hover:bg-white/8'
          }`}
        >
          {muted ? (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="1" y1="1" x2="23" y2="23"/>
              <path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/>
              <path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23"/>
              <line x1="12" y1="19" x2="12" y2="23"/>
            </svg>
          ) : (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="23"/>
            </svg>
          )}
        </button>

        <button
          onClick={() => navigate(`/session/${bot.id}`)}
          className="text-xs font-medium text-slate-300 hover:text-white px-3 py-2 rounded-xl hover:bg-white/8 transition-all shrink-0"
        >
          Back to call
        </button>

        <button
          onClick={endCall}
          className="text-xs font-semibold text-white bg-red-600/80 hover:bg-red-500 px-3.5 py-2 rounded-xl transition-all shrink-0"
        >
          End
        </button>
      </div>
    </div>
  )
}
