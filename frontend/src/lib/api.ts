const BASE = ''  // proxied through Vite to localhost:8080

function authHeaders(): HeadersInit {
  const token = localStorage.getItem('token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// Task 2.5 — the access token now lives 15 minutes (was 60), so a session
// staying open longer than that needs this to keep working transparently.
// One in-flight refresh at a time even if several requests 401
// simultaneously (e.g. a page that fires a few API calls at once right as
// the token expires) — without this they'd each kick off their own
// /auth/refresh, and since the refresh token rotates on every use (server
// side), only the first would win and the rest would fail.
let refreshInFlight: Promise<string> | null = null

async function refreshAccessToken(): Promise<string> {
  if (!refreshInFlight) {
    refreshInFlight = fetch(BASE + '/auth/refresh', { method: 'POST', credentials: 'include' })
      .then(async res => {
        if (!res.ok) throw new Error('Session expired')
        const { access_token } = await res.json()
        localStorage.setItem('token', access_token)
        return access_token as string
      })
      .finally(() => { refreshInFlight = null })
  }
  return refreshInFlight
}

async function request<T>(path: string, options: RequestInit = {}, _retried = false): Promise<T> {
  const res = await fetch(BASE + path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...options.headers },
  })
  if (res.status === 401 && !_retried) {
    // The access token expired mid-session (routine, not an error the user
    // needs to see) — silently refresh and retry this one request once.
    try {
      await refreshAccessToken()
      return request<T>(path, options, true)
    } catch {
      // Refresh token is also gone/expired/revoked — genuinely logged out.
      // AuthContext's own logout() clears local state; here just surface
      // the original failure so the caller's error handling takes over.
    }
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Request failed')
  }
  return res.json()
}

// ── Auth ────────────────────────────────────────────────────────────────────
export async function register(email: string, password: string) {
  return request('/auth/register', { method: 'POST', body: JSON.stringify({ email, password }) })
}

export async function login(email: string, password: string): Promise<{ access_token: string }> {
  const res = await fetch(BASE + '/auth/login', {
    method: 'POST',
    credentials: 'include',  // receives the httpOnly refresh cookie
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Login failed')
  }
  return res.json()
}

export async function logout(): Promise<void> {
  // Best-effort: the point is revoking the refresh token server-side so a
  // captured copy of it stops working, but a failed network call here
  // shouldn't block the user from being logged out locally.
  await fetch(BASE + '/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {})
}

// Attempts a silent refresh using whatever refresh cookie the browser
// already has (e.g. on app load after closing and reopening the tab) —
// lets a real session survive a page reload past the 15-minute access
// token lifetime without forcing a fresh login. Resolves to null rather
// than throwing when there's no valid session to resume.
export async function trySilentRefresh(): Promise<string | null> {
  try {
    return await refreshAccessToken()
  } catch {
    return null
  }
}

// ── Bots ────────────────────────────────────────────────────────────────────
export interface Bot {
  id: string
  name: string
  system_prompt: string
  voice_id: string
  llm_model: string
  language: string
}

export async function listBots(): Promise<Bot[]> {
  return request('/bots/')
}

export async function createBot(data: Omit<Bot, 'id'>): Promise<Bot> {
  return request('/bots/', { method: 'POST', body: JSON.stringify(data) })
}

export async function updateBot(id: string, data: Partial<Omit<Bot, 'id'>>): Promise<Bot> {
  return request(`/bots/${id}`, { method: 'PATCH', body: JSON.stringify(data) })
}

export async function deleteBot(id: string): Promise<void> {
  return request(`/bots/${id}`, { method: 'DELETE' })
}

// ── Documents ───────────────────────────────────────────────────────────────
export interface BotDocument {
  id: string
  filename: string
  chunk_count: number
  created_at: string
}

export async function listDocuments(botId: string): Promise<BotDocument[]> {
  return request(`/bots/${botId}/documents`)
}

export async function uploadDocument(botId: string, file: File): Promise<BotDocument> {
  const formData = new FormData()
  formData.append('file', file)
  const token = localStorage.getItem('token')
  const res = await fetch(`/bots/${botId}/documents`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Upload failed')
  }
  return res.json()
}

export async function deleteDocument(docId: string): Promise<void> {
  return request(`/documents/${docId}`, { method: 'DELETE' })
}

// ── WebRTC connect ──────────────────────────────────────────────────────────
export async function connectBot(
  botId: string, sdp: string, type: string, pcId?: string
): Promise<{ sdp: string; type: string; pc_id: string }> {
  return request('/connect', {
    method: 'POST',
    body: JSON.stringify({ bot_id: botId, sdp, type, pc_id: pcId ?? null }),
  })
}
