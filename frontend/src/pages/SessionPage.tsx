import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { listBots } from '../lib/api'
import type { Bot } from '../lib/api'
import { useCall, isCallActive } from '../context/CallContext'
import { usePageChrome } from '../context/ChromeContext'

/**
 * A VIEW of the call, not the owner of it.
 *
 * Everything to do with WebRTC moved to CallContext, which lives above the
 * router. This page renders whatever that context says and sends clicks
 * back to it. The practical difference: leaving this page no longer hangs
 * up. There is no cleanup here that ends anything.
 */
export default function SessionPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [bot, setBot] = useState<Bot | null>(null)
  const {
    bot: callBot, status, speaking, muted, log, sources, openingDoc,
    startCall, endCall, toggleMute, openSource,
  } = useCall()

  // No background blobs: this page paints its own, and it reacts to the
  // caller's voice.
  usePageChrome(bot?.name ?? 'Call', '/dashboard', false)

  useEffect(() => {
    listBots().then(bots => setBot(bots.find(b => b.id === id) ?? null))
  }, [id])

  // Is the live call THIS bot's call? A call now survives navigation, so
  // arriving here while a different bot is on the line is a real state the
  // page has to have an answer for.
  const isThisCall = callBot?.id === id
  const otherCallLive = isCallActive(status) && !isThisCall

  // What this page should show. A call to another bot leaves this page idle
  // — its own call has not started.
  const shown = isThisCall ? status : 'idle'
  const isActive = shown === 'connected'
  const isConnecting = shown === 'connecting'

  return (
    <div className="flex flex-col relative overflow-hidden min-h-[calc(100vh-65px)]">
      {/* Ambient glow that reacts to speaking */}
      <div className={`absolute inset-0 transition-all duration-700 pointer-events-none ${
        isActive && speaking
          ? 'bg-emerald-500/5'
          : isActive
          ? 'bg-violet-500/3'
          : 'bg-transparent'
      }`} />
      <div className={`absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full blur-[100px] pointer-events-none transition-all duration-700 ${
        isActive && speaking
          ? 'w-[600px] h-[600px] bg-emerald-500/12'
          : isActive
          ? 'w-[500px] h-[500px] bg-violet-500/10'
          : 'w-[300px] h-[300px] bg-slate-700/10'
      }`} />

      <main className="relative z-10 flex-1 flex flex-col items-center justify-center gap-10 p-6">

        {/* Status pill — moved out of the header, which the shell now owns */}
        <span className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full font-medium ${
          shown === 'connected' ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/20'
          : shown === 'connecting' ? 'bg-amber-500/15 text-amber-400 border border-amber-500/20'
          : shown === 'error' ? 'bg-red-500/15 text-red-400 border border-red-500/20'
          : 'bg-white/8 text-slate-400 border border-white/10'
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${
            shown === 'connected' ? 'bg-emerald-400 animate-pulse'
            : shown === 'connecting' ? 'bg-amber-400 animate-pulse'
            : shown === 'error' ? 'bg-red-400'
            : 'bg-slate-500'
          }`} />
          {shown === 'connected' ? 'Live' : shown === 'connecting' ? 'Connecting…' : shown === 'error' ? 'Error' : 'Ready'}
        </span>

        {/* Orb */}
        <div className="relative flex items-center justify-center select-none">
          {/* Outer ring — speaking pulse */}
          {isActive && speaking && (
            <>
              <div className="absolute w-64 h-64 rounded-full border border-emerald-500/20 animate-ping" style={{ animationDuration: '1.5s' }} />
              <div className="absolute w-52 h-52 rounded-full border border-emerald-500/30 animate-ping" style={{ animationDuration: '1s' }} />
            </>
          )}
          {isConnecting && (
            <div className="absolute w-52 h-52 rounded-full border-2 border-violet-500/30 border-t-violet-500 animate-spin" />
          )}

          {/* Orb body */}
          <div className={`relative w-40 h-40 rounded-full flex items-center justify-center transition-all duration-500 ${
            isActive && speaking
              ? 'scale-110'
              : isActive
              ? 'scale-100'
              : 'scale-95'
          }`}>
            {/* Gradient background */}
            <div className={`absolute inset-0 rounded-full transition-all duration-500 ${
              isActive && speaking
                ? 'bg-gradient-to-br from-emerald-400 to-teal-600 shadow-[0_0_60px_rgba(52,211,153,0.4)]'
                : isActive
                ? 'bg-gradient-to-br from-violet-500 to-indigo-700 shadow-[0_0_60px_rgba(139,92,246,0.3)]'
                : isConnecting
                ? 'bg-gradient-to-br from-amber-500/60 to-orange-700/60'
                : 'bg-gradient-to-br from-slate-700 to-slate-800'
            }`} />

            {/* Mic icon */}
            <div className="relative z-10">
              {isActive && !muted ? (
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                  <line x1="12" y1="19" x2="12" y2="23"/>
                  <line x1="8" y1="23" x2="16" y2="23"/>
                </svg>
              ) : (
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="1" y1="1" x2="23" y2="23"/>
                  <path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/>
                  <path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23"/>
                  <line x1="12" y1="19" x2="12" y2="23"/>
                  <line x1="8" y1="23" x2="16" y2="23"/>
                </svg>
              )}
            </div>

            {/* Speaking wave bars */}
            {isActive && speaking && (
              <div className="absolute -bottom-8 flex items-end gap-1 justify-center">
                {[3, 5, 8, 5, 3].map((h, i) => (
                  <div
                    key={i}
                    className="w-1 bg-emerald-400 rounded-full animate-bounce"
                    style={{ height: `${h * 2}px`, animationDelay: `${i * 0.1}s`, animationDuration: '0.6s' }}
                  />
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Status text */}
        <div className="text-center mt-2">
          <p className="text-slate-300 font-medium">
            {otherCallLive && `You're already on a call with ${callBot?.name}`}
            {!otherCallLive && shown === 'idle' && 'Press Start to begin'}
            {!otherCallLive && shown === 'connecting' && 'Setting up connection…'}
            {!otherCallLive && shown === 'connected' && muted && 'Your microphone is muted'}
            {!otherCallLive && shown === 'connected' && !muted && speaking && `${bot?.name ?? 'Bot'} is listening…`}
            {!otherCallLive && shown === 'connected' && !muted && !speaking && 'Speak naturally — the bot will reply'}
            {!otherCallLive && shown === 'error' && 'Something went wrong'}
          </p>
          {otherCallLive ? (
            <p className="text-xs text-slate-600 mt-1">End that call before starting this one.</p>
          ) : isActive ? (
            <p className="text-xs text-slate-600 mt-1">
              Pause naturally to let the bot respond. You can browse the app — the call stays up.
            </p>
          ) : null}
        </div>

        {/* Controls */}
        <div className="flex gap-3">
          {otherCallLive ? (
            <>
              <button
                onClick={() => navigate(`/session/${callBot!.id}`)}
                className="flex items-center gap-2.5 bg-white/8 hover:bg-white/12 text-white font-semibold px-6 py-3 rounded-2xl transition-all text-sm"
              >
                Go to that call
              </button>
              <button
                onClick={endCall}
                className="flex items-center gap-2.5 bg-red-600/80 hover:bg-red-500 text-white font-semibold px-6 py-3 rounded-2xl transition-all shadow-lg shadow-red-900/30 text-sm"
              >
                End it
              </button>
            </>
          ) : shown === 'idle' || shown === 'error' ? (
            <button
              onClick={() => bot && startCall(bot)}
              disabled={!bot}
              className="flex items-center gap-2.5 bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 disabled:opacity-50 text-white font-semibold px-8 py-3 rounded-2xl transition-all shadow-lg shadow-violet-900/40 text-sm"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              </svg>
              Start Session
            </button>
          ) : (
            <>
              <button
                onClick={toggleMute}
                disabled={shown !== 'connected'}
                aria-pressed={muted}
                className={`flex items-center gap-2.5 font-semibold px-6 py-3 rounded-2xl transition-all text-sm disabled:opacity-40 ${
                  muted
                    ? 'bg-red-500/15 text-red-300 hover:bg-red-500/25'
                    : 'bg-white/8 text-slate-200 hover:bg-white/12'
                }`}
              >
                {muted ? 'Unmute' : 'Mute'}
              </button>
              <button
                onClick={endCall}
                className="flex items-center gap-2.5 bg-red-600/80 hover:bg-red-500 text-white font-semibold px-8 py-3 rounded-2xl transition-all shadow-lg shadow-red-900/30 text-sm"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                  <rect x="3" y="3" width="18" height="18" rx="2"/>
                </svg>
                End Session
              </button>
            </>
          )}
        </div>

        {/* Sources — Task 2.10 */}
        {isThisCall && sources !== null && (
          <div className="w-full max-w-sm bg-white/3 border border-white/8 rounded-2xl p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold mb-3">
              {sources.length > 0 ? 'Answered from' : 'Source'}
            </p>

            {sources.length === 0 ? (
              /* An empty list is a real answer, not a missing one: the bot
                 replied from the model's own knowledge rather than from the
                 customer's documents. Saying so plainly is the point — it's
                 the difference between an answer they can verify and one
                 they can't. */
              <div className="flex items-start gap-2.5">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2" strokeLinecap="round" className="text-amber-400/70 mt-0.5 shrink-0">
                  <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
                  <line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
                <p className="text-xs text-slate-400 leading-relaxed">
                  General knowledge — not from your documents.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {sources.map((src, i) => {
                  const clickable = src.has_file && !!src.doc_id
                  const busy = openingDoc === src.doc_id
                  return (
                    <button
                      key={`${src.doc_id}-${src.page}-${i}`}
                      onClick={() => openSource(src)}
                      disabled={!clickable || busy}
                      title={clickable
                        ? `Open ${src.filename}${src.page ? ` at page ${src.page}` : ''}`
                        : 'The original file was not stored for this document — re-upload it to enable opening'}
                      className={`w-full flex items-center gap-2.5 text-left px-2.5 py-2 rounded-xl border transition-all ${
                        clickable
                          ? 'border-white/8 hover:border-violet-500/40 hover:bg-violet-500/8 cursor-pointer'
                          : 'border-white/5 cursor-default'
                      }`}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                           strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                           className="text-slate-500 shrink-0">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                      </svg>

                      <span className="min-w-0 flex-1">
                        <span className="block text-xs text-slate-300 truncate">{src.filename}</span>
                        {src.page !== null && (
                          <span className="block text-[11px] text-slate-500">Page {src.page}</span>
                        )}
                      </span>

                      {/* Only shown when the reranker actually produced a
                          score. On the fallback path the backend sends null
                          rather than a raw cosine value, which sits on a
                          different scale and would misrepresent confidence. */}
                      {src.score !== null && (
                        <span className="text-[10px] text-slate-600 tabular-nums shrink-0">
                          {Math.round(src.score * 100)}%
                        </span>
                      )}

                      {busy && (
                        <span className="w-3 h-3 rounded-full border border-violet-400/40 border-t-violet-400 animate-spin shrink-0" />
                      )}
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {/* Log */}
        {isThisCall && log.length > 0 && (
          <div className="w-full max-w-sm bg-white/3 border border-white/8 rounded-2xl p-4 font-mono text-xs text-slate-500 space-y-1.5 max-h-36 overflow-y-auto">
            {log.map((l, i) => (
              <div key={i} className={l.includes('✓') ? 'text-emerald-500' : l.includes('Error') || l.includes('lost') ? 'text-red-400' : ''}>
                {l}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
