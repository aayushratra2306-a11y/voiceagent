import { useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { login, register } from '../lib/api'
import { useAuth } from '../context/AuthContext'

export default function LoginPage() {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const { login: saveToken } = useAuth()
  const navigate = useNavigate()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      if (mode === 'register') {
        await register(email, password)
        setMode('login')
        setError('Account created! Please sign in.')
      } else {
        const { access_token } = await login(email, password)
        saveToken(access_token)
        navigate('/dashboard')
      }
    } catch (err: any) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-[#070711] flex items-center justify-center p-4 relative overflow-hidden">
      {/* Background glow blobs */}
      <div className="absolute top-[-20%] left-[-10%] w-[500px] h-[500px] rounded-full bg-violet-700/20 blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-20%] right-[-10%] w-[500px] h-[500px] rounded-full bg-indigo-700/20 blur-[120px] pointer-events-none" />

      <div className="w-full max-w-sm relative z-10">
        {/* Brand */}
        <div className="mb-7 text-center">
          {/* Logomark + wordmark */}
          <div className="flex items-center justify-center gap-2.5 mb-4">
            {/* Equalizer bars mark */}
            <svg width="26" height="26" viewBox="0 0 26 26" fill="none">
              <rect x="0"  y="14" width="5" height="12" rx="2.5" fill="#00D4FF" opacity="0.5"/>
              <rect x="7"  y="7"  width="5" height="19" rx="2.5" fill="#00D4FF" opacity="0.75"/>
              <rect x="14" y="2"  width="5" height="24" rx="2.5" fill="#00D4FF"/>
              <rect x="21" y="9"  width="5" height="17" rx="2.5" fill="#00D4FF" opacity="0.6"/>
            </svg>
            <span style={{ fontFamily: "'Barlow Condensed', sans-serif", fontWeight: 900, fontSize: '2.4rem', letterSpacing: '0.07em', color: '#00D4FF', lineHeight: 1, textShadow: '0 0 28px rgba(0,212,255,0.28)' }}>
              AURIS
            </span>
          </div>

          {/* Brand copy */}
          <p className="text-white font-medium text-[0.95rem] mb-1.5">Intelligence you can talk to.</p>
          <p className="text-slate-500 text-[0.8rem] leading-relaxed max-w-[260px] mx-auto">
            Build AI voice agents with distinct personalities and speak with them naturally — no typing, no waiting. Just conversation.
          </p>

          {/* Thin rule */}
          <div className="mt-5 border-t border-white/6" />
          <p className="text-xs text-slate-600 mt-4">
            {mode === 'login' ? 'Sign in to your account' : 'Create a new account'}
          </p>
        </div>

        {/* Card */}
        <div className="bg-white/5 border border-white/10 rounded-2xl p-6 backdrop-blur-sm shadow-xl">
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div className={`text-sm rounded-xl px-3.5 py-2.5 ${error.includes('created') ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-red-500/10 text-red-400 border border-red-500/20'}`}>
                {error}
              </div>
            )}

            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1.5">Email address</label>
              <input
                type="email"
                required
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="w-full bg-white/5 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white placeholder-slate-600 outline-none focus:border-violet-500/60 focus:ring-1 focus:ring-violet-500/30 transition-all"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1.5">Password</label>
              <input
                type="password"
                required
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full bg-white/5 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white placeholder-slate-600 outline-none focus:border-violet-500/60 focus:ring-1 focus:ring-violet-500/30 transition-all"
              />
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 disabled:opacity-50 text-white font-semibold rounded-xl py-2.5 text-sm transition-all shadow-lg shadow-violet-900/30 mt-1"
            >
              {loading ? 'Please wait…' : mode === 'login' ? 'Sign In' : 'Create Account'}
            </button>
          </form>
        </div>

        <p className="text-center text-sm text-slate-500 mt-5">
          {mode === 'login' ? "Don't have an account? " : 'Already have an account? '}
          <button
            onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError('') }}
            className="text-violet-400 hover:text-violet-300 font-medium transition-colors"
          >
            {mode === 'login' ? 'Sign up' : 'Sign in'}
          </button>
        </p>
      </div>
    </div>
  )
}
