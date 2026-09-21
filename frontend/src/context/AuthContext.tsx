import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { logout as logoutApi, trySilentRefresh } from '../lib/api'

interface AuthContextType {
  token: string | null
  ready: boolean
  /**
   * The signed-in user's email, read from the access token's `sub` claim
   * (verified against `create_access_token` / `get_current_user` in
   * backend/app/core/auth.py, which mint and read `sub` as the email).
   * Only for display and for matching yourself in a members list — every
   * decision that matters is the server's. Never trust a decoded token
   * for anything but this.
   */
  email: string | null
  login: (token: string) => void
  logout: () => void
}

const AuthContext = createContext<AuthContextType | null>(null)

function emailFromToken(token: string | null): string | null {
  if (!token) return null
  try {
    const payload = JSON.parse(atob(token.split('.')[1] ?? ''))
    return typeof payload?.sub === 'string' ? payload.sub : null
  } catch {
    return null
  }
}

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
    // Not awaited, and that is safe as of review finding I19: logoutApi()
    // invalidates any in-flight refresh and clears the stored token
    // synchronously, before it awaits anything. Only the server-side
    // revocation is left to finish in the background, so a slow or failed
    // network call can no longer leave a usable token behind.
    logoutApi()
    localStorage.removeItem('token')
    setToken(null)
  }

  const email = useMemo(() => emailFromToken(token), [token])

  return <AuthContext.Provider value={{ token, ready, email, login, logout }}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
