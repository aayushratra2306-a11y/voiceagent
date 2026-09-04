import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listBots, deleteBot } from '../lib/api'
import type { Bot } from '../lib/api'
import { useAuth } from '../context/AuthContext'

export default function DashboardPage() {
  const [bots, setBots] = useState<Bot[]>([])
  const [loading, setLoading] = useState(true)
  const { logout } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    listBots()
      .then(setBots)
      .catch(() => navigate('/'))
      .finally(() => setLoading(false))
  }, [])

  async function handleDelete(id: string) {
    if (!confirm('Delete this bot?')) return
    await deleteBot(id)
    setBots(prev => prev.filter(b => b.id !== id))
  }

  return (
    <div className="min-h-screen bg-[#070711] text-white relative overflow-hidden">
      {/* Background blobs */}
      <div className="absolute top-[-15%] right-[-10%] w-[500px] h-[500px] rounded-full bg-violet-700/15 blur-[130px] pointer-events-none" />
      <div className="absolute bottom-[-20%] left-[-5%] w-[400px] h-[400px] rounded-full bg-indigo-700/15 blur-[120px] pointer-events-none" />

      {/* Header */}
      <header className="relative z-10 border-b border-white/8 px-6 py-4 flex items-center justify-between backdrop-blur-sm">
        <div className="flex items-center gap-2">
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
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => navigate('/approvals')}
            className="text-xs text-slate-500 hover:text-slate-300 transition-colors px-3 py-1.5 rounded-lg hover:bg-white/5"
          >
            Approvals
          </button>
          <button
            onClick={() => navigate('/webhooks')}
            className="text-xs text-slate-500 hover:text-slate-300 transition-colors px-3 py-1.5 rounded-lg hover:bg-white/5"
          >
            Webhooks
          </button>
          <button
            onClick={() => { logout(); navigate('/') }}
            className="text-xs text-slate-500 hover:text-slate-300 transition-colors px-3 py-1.5 rounded-lg hover:bg-white/5"
          >
            Sign out
          </button>
        </div>
      </header>

      <main className="relative z-10 max-w-3xl mx-auto px-6 py-10">
        <div className="flex items-center justify-between mb-7">
          <div>
            <h2 className="text-xl font-bold text-white">Your Bots</h2>
            <p className="text-sm text-slate-500 mt-0.5">Create and manage your voice assistants</p>
          </div>
          <button
            onClick={() => navigate('/bots/new')}
            className="flex items-center gap-2 bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white text-sm font-semibold px-4 py-2.5 rounded-xl transition-all shadow-lg shadow-violet-900/30"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            New Bot
          </button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-24">
            <div className="w-6 h-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : bots.length === 0 ? (
          <div className="border border-dashed border-white/10 rounded-2xl p-16 text-center bg-white/2">
            <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-white/5 border border-white/10 mb-4">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"/><path d="M8 9.05v-.1"/><path d="M16 9.05v-.1"/>
                <path d="M11.5 14a3.5 3.5 0 0 0 1 0"/>
              </svg>
            </div>
            <p className="text-slate-300 font-medium">No bots yet</p>
            <p className="text-slate-500 text-sm mt-1 mb-5">Create your first voice assistant to get started</p>
            <button
              onClick={() => navigate('/bots/new')}
              className="bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white text-sm font-semibold px-5 py-2.5 rounded-xl transition-all shadow-lg shadow-violet-900/30"
            >
              Create Bot
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {bots.map(bot => (
              <div
                key={bot.id}
                className="group bg-white/4 border border-white/8 hover:border-violet-500/30 rounded-2xl px-5 py-4 flex items-center justify-between transition-all hover:bg-white/6"
              >
                <div className="flex items-center gap-3.5 min-w-0">
                  <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-600/30 to-indigo-600/30 border border-violet-500/20 flex items-center justify-center shrink-0">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                      <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                    </svg>
                  </div>
                  <div className="min-w-0">
                    <p className="font-semibold text-white">{bot.name}</p>
                    <p className="text-xs text-slate-500 mt-0.5 truncate max-w-xs">{bot.system_prompt}</p>
                  </div>
                </div>

                <div className="flex items-center gap-2 ml-4 shrink-0">
                  <button
                    onClick={() => navigate(`/session/${bot.id}`)}
                    className="flex items-center gap-1.5 bg-emerald-600/80 hover:bg-emerald-500 text-white text-xs font-semibold px-3.5 py-2 rounded-xl transition-all shadow-sm"
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                      <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                    </svg>
                    Talk
                  </button>
                  <button
                    onClick={() => navigate(`/bots/${bot.id}`)}
                    className="p-2 text-slate-500 hover:text-slate-200 hover:bg-white/8 rounded-xl transition-all"
                    title="Settings"
                  >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
                    </svg>
                  </button>
                  <button
                    onClick={() => handleDelete(bot.id)}
                    className="p-2 text-slate-500 hover:text-red-400 hover:bg-red-500/10 rounded-xl transition-all"
                    title="Delete"
                  >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
                    </svg>
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
