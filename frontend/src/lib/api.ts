const BASE = ''  // proxied through Vite to localhost:8080

function authHeaders(): HeadersInit {
  const token = localStorage.getItem('token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(BASE + path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...options.headers },
  })
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
  return request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })
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
