import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { logout as logoutApi, trySilentRefresh } from '../lib/api'

interface AuthContextType {
  token: string | null
  ready: boolean
  login: (token: string) => void
  logout: () => void
}

const AuthContext = createContext<AuthContextType | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(localStorage.getItem('token'))
  // Task 2.5 — the access token now lives 15 minutes, so whatever's sitting
  // in localStorage from a previous visit has very likely already expired
  // by the time the app reloads. Try to silently exchange the httpOnly
  // refresh cookie for a fresh one before rendering anything that assumes
  // `token` is actually valid — `ready` gates that render.
  const [ready, setReady] = useState(false)

  useEffect(() => {
    trySilentRefresh().then(fresh => {
      if (fresh) setToken(fresh)
      else if (token) {
        // Stored token but no valid refresh cookie (expired, revoked, or
        // this is a browser that never had one) — it's stale; don't keep
        // pretending it's a live session.
        localStorage.removeItem('token')
        setToken(null)
      }
      setReady(true)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function login(t: string) {
    localStorage.setItem('token', t)
    setToken(t)
  }

  function logout() {
    logoutApi()  // best-effort server-side revocation; don't block on it
    localStorage.removeItem('token')
    setToken(null)
  }

  return <AuthContext.Provider value={{ token, ready, login, logout }}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
