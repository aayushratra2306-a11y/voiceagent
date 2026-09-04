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

// ── Bot tools (Task 3.1) ────────────────────────────────────────────────────
// A tool is a database record rather than code: a name, a description the AI
// reads to decide when to use it, the inputs it must supply, and where to send
// them. The credential is write-only across this API — it goes out in `secret`
// and comes back only as `secret_masked`.
export interface ToolParameter {
  name: string
  type: 'string' | 'number' | 'integer' | 'boolean'
  description: string
  required: boolean
}

export interface BotTool {
  id: string
  name: string
  description: string
  enabled: boolean
  kind: 'http' | 'builtin'
  builtin: string
  method: string
  url: string
  headers: Record<string, string>
  query: Record<string, string>
  body: Record<string, unknown>
  parameters: ToolParameter[]
  auth: { kind: string; name: string; secret_masked: string; has_secret: boolean }
}

/** What the form sends. Omitting `auth.secret` means "keep the stored one",
 *  which is what lets the URL be edited without re-typing the API key. */
export type BotToolInput = Omit<BotTool, 'id' | 'auth'> & {
  auth: { kind: string; name: string; secret?: string }
}

export async function listTools(botId: string): Promise<BotTool[]> {
  return request(`/bots/${botId}/tools/`)
}

export async function createTool(botId: string, data: BotToolInput): Promise<BotTool> {
  return request(`/bots/${botId}/tools/`, { method: 'POST', body: JSON.stringify(data) })
}

export async function updateTool(botId: string, toolId: string, data: BotToolInput): Promise<BotTool> {
  return request(`/bots/${botId}/tools/${toolId}`, { method: 'PATCH', body: JSON.stringify(data) })
}

export async function deleteTool(botId: string, toolId: string): Promise<void> {
  return request(`/bots/${botId}/tools/${toolId}`, { method: 'DELETE' })
}

/** Run a tool once, now, without placing a call — so a wrong URL or key is
 *  found at configuration time rather than mid-conversation. */
export async function testTool(
  botId: string,
  toolId: string,
  args: Record<string, string>,
): Promise<Record<string, unknown>> {
  return request(`/bots/${botId}/tools/${toolId}/test`, {
    method: 'POST',
    body: JSON.stringify(args),
  })
}

// ── Documents ───────────────────────────────────────────────────────────────
export interface BotDocument {
  id: string
  filename: string
  chunk_count: number
  created_at: string
  // Task 2.10 — false for documents uploaded before original files were
  // stored. They still cite correctly, they just can't be opened.
  has_file?: boolean
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

// Task 2.10 — fetch a source PDF as a blob URL so a citation can open it.
//
// Deliberately NOT a plain <a href="/documents/x/file"> link: that route
// requires the Authorization header (it serves a customer's own uploaded
// documents), and a browser navigation can't send one. Putting the token
// in the query string instead would leak it into history and server logs.
// So: fetch it with the header, hand the browser a blob.
//
// Doesn't go through request() because the response is binary, not JSON —
// but it repeats the same 401-refresh-once behavior so a citation clicked
// after the 15-minute access token expires still opens.
export async function fetchDocumentBlobUrl(docId: string, _retried = false): Promise<string> {
  const res = await fetch(`${BASE}/documents/${docId}/file`, { headers: authHeaders() })

  if (res.status === 401 && !_retried) {
    await refreshAccessToken()
    return fetchDocumentBlobUrl(docId, true)
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Could not open document')
  }

  return URL.createObjectURL(await res.blob())
}

// ── WebRTC connect ──────────────────────────────────────────────────────────

// Task 2.3 — ICE config comes from the server, not a hardcoded list here.
// Until 2026-09-01 this page pinned three Google STUN servers, so the TURN
// relay the backend deploys was invisible to the browser: the one peer that
// actually needs a relay never knew there was one. Fetching it means a TURN
// change is a server-side .env edit, not a frontend rebuild.
export async function getIceServers(): Promise<RTCIceServer[]> {
  const data: { iceServers: RTCIceServer[] } = await request('/connect/ice-servers')
  return data.iceServers
}

// Trickle ICE (added 2026-09-03). The browser used to wait for ICE gathering
// to FINISH before sending its offer — up to a 5 second timeout, paid on every
// single call before the caller heard anything. Gathering is slowest exactly
// when a TURN server is configured, because the relay allocation is a network
// round trip of its own, so the wait was longest for the users who need the
// relay most.
//
// The backend has accepted trickled candidates all along (POST /connect/ice,
// routed to that call's worker) — the frontend simply never used it. Now the
// offer goes immediately and candidates follow as they are discovered, which
// is what trickle ICE is for.
export async function sendIceCandidates(
  pcId: string, candidates: RTCIceCandidate[],
): Promise<void> {
  if (!candidates.length) return
  // Best-effort: a dropped candidate degrades connectivity, it does not break
  // the call, and throwing here would surface as a session failure to the user.
  await request('/connect/ice', {
    method: 'POST',
    body: JSON.stringify({
      pc_id: pcId,
      candidates: candidates.map(c => ({
        candidate: c.candidate,
        sdp_mid: c.sdpMid ?? '0',
        sdp_mline_index: c.sdpMLineIndex ?? 0,
      })),
    }),
  }).catch(() => {})
}

export async function connectBot(
  botId: string, sdp: string, type: string, pcId?: string
): Promise<{ sdp: string; type: string; pc_id: string }> {
  return request('/connect', {
    method: 'POST',
    body: JSON.stringify({ bot_id: botId, sdp, type, pc_id: pcId ?? null }),
  })
}
