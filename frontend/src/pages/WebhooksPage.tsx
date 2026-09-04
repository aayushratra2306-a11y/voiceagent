import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  listWebhookEvents, listWebhookSubscriptions, createWebhookSubscription,
  updateWebhookSubscription, deleteWebhookSubscription, listWebhookDeliveries,
  testWebhookSubscription,
} from '../lib/api'
import type { WebhookSubscription, WebhookSubscriptionInput, WebhookDeliveryLogEntry } from '../lib/api'

// Task 3.8 — a customer registering their own URL per event, the manual's
// second step. Deliberately account-wide rather than per-bot: an event like
// "a call ended" is something a customer's OWN system wants to hear about
// regardless of which of their bots it came from.

const BLANK: WebhookSubscriptionInput = { event: '', url: '', enabled: true, secret: '' }

const card = 'bg-white/4 border border-white/8 rounded-2xl p-5'
const label = 'block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2'
const field = 'w-full bg-white/5 border border-white/10 rounded-xl px-3 py-2.5 text-sm text-white placeholder-slate-600 outline-none focus:border-violet-500/60 transition-all'

export default function WebhooksPage() {
  const navigate = useNavigate()

  const [events, setEvents] = useState<string[]>([])
  const [subs, setSubs] = useState<WebhookSubscription[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [editingId, setEditingId] = useState<string | null>(null)
  const [form, setForm] = useState<WebhookSubscriptionInput>(BLANK)
  const [secretTouched, setSecretTouched] = useState(false)
  const [saving, setSaving] = useState(false)

  const [logFor, setLogFor] = useState<string | null>(null)
  const [log, setLog] = useState<WebhookDeliveryLogEntry[]>([])
  const [testResult, setTestResult] = useState<Record<string, string>>({})
  const [testing, setTesting] = useState<string | null>(null)

  useEffect(() => { refresh() }, [])

  async function refresh() {
    try {
      const [e, s] = await Promise.all([listWebhookEvents(), listWebhookSubscriptions()])
      setEvents(e); setSubs(s)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  function startNew() {
    setEditingId('new'); setForm({ ...BLANK, event: events[0] ?? '' }); setSecretTouched(false)
  }

  function startEdit(s: WebhookSubscription) {
    setEditingId(s.id)
    // secret deliberately absent — the API never returns it, and omitting
    // it here tells the server to keep the stored one on save.
    setForm({ event: s.event, url: s.url, enabled: s.enabled })
    setSecretTouched(false)
  }

  async function save() {
    setSaving(true); setError('')
    const payload = secretTouched ? form : { ...form, secret: undefined }
    try {
      if (editingId === 'new') await createWebhookSubscription(payload)
      else await updateWebhookSubscription(editingId!, payload)
      setEditingId(null)
      await refresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function remove(s: WebhookSubscription) {
    if (!confirm(`Stop sending "${s.event}" to ${s.url}?`)) return
    try { await deleteWebhookSubscription(s.id); await refresh() } catch (e: any) { setError(e.message) }
  }

  async function openLog(s: WebhookSubscription) {
    setLogFor(s.id)
    try { setLog(await listWebhookDeliveries(s.id)) } catch (e: any) { setError(e.message) }
  }

  async function runTest(s: WebhookSubscription) {
    setTesting(s.id)
    try {
      const result = await testWebhookSubscription(s.id)
      setTestResult(prev => ({ ...prev, [s.id]: result.ok ? 'Delivered — check the log for details' : `Failed: ${result.error}` }))
    } catch (e: any) {
      setTestResult(prev => ({ ...prev, [s.id]: `Could not send: ${e.message}` }))
    } finally {
      setTesting(null)
    }
  }

  if (loading) return (
    <div className="min-h-screen bg-[#070711] flex items-center justify-center">
      <div className="w-6 h-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
    </div>
  )

  return (
    <div className="min-h-screen bg-[#070711] text-white relative overflow-hidden">
      <div className="absolute top-[-15%] right-[-10%] w-[500px] h-[500px] rounded-full bg-violet-700/15 blur-[130px] pointer-events-none" />

      <header className="relative z-10 border-b border-white/8 px-6 py-4 flex items-center gap-3 backdrop-blur-sm">
        <button onClick={() => navigate('/dashboard')} className="text-slate-500 hover:text-white transition-colors">←</button>
        <h1 className="text-sm font-semibold text-slate-200">Webhooks</h1>
      </header>

      <main className="relative z-10 max-w-2xl mx-auto px-6 py-10 space-y-6">
        <p className="text-sm text-slate-400">
          Get notified the moment something happens — a call ends, an appointment is booked — on
          your own system. Every notification is signed so you can verify it really came from here.
        </p>

        {error && (
          <div className="bg-red-500/10 border border-red-500/25 rounded-xl px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {subs.length === 0 && editingId !== 'new' && (
          <div className={`${card} text-center text-sm text-slate-500`}>
            Nothing is subscribed yet — add one below.
          </div>
        )}

        <div className="space-y-3">
          {subs.map(s => (
            <div key={s.id} className={card}>
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm text-violet-300">{s.event}</span>
                    {!s.enabled && <span className="text-xs text-slate-600">(paused)</span>}
                  </div>
                  <p className="text-xs text-slate-500 truncate mt-0.5">{s.url}</p>
                </div>
                <div className="flex gap-1 shrink-0">
                  <button onClick={() => runTest(s)} disabled={testing === s.id}
                    className="text-xs px-3 py-1.5 rounded-lg bg-white/6 hover:bg-white/12 text-slate-300 disabled:opacity-50">
                    {testing === s.id ? 'Sending…' : 'Send test'}
                  </button>
                  <button onClick={() => openLog(s)}
                    className="text-xs px-3 py-1.5 rounded-lg bg-white/6 hover:bg-white/12 text-slate-300">
                    Log
                  </button>
                  <button onClick={() => startEdit(s)}
                    className="text-xs px-3 py-1.5 rounded-lg bg-white/6 hover:bg-white/12 text-slate-300">
                    Edit
                  </button>
                  <button onClick={() => remove(s)}
                    className="text-xs px-3 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-300">
                    Delete
                  </button>
                </div>
              </div>

              {testResult[s.id] && (
                <p className="text-xs text-slate-400 mt-2 pt-2 border-t border-white/8">{testResult[s.id]}</p>
              )}

              {logFor === s.id && (
                <div className="mt-3 pt-3 border-t border-white/8 space-y-1.5 max-h-56 overflow-y-auto">
                  {log.length === 0 && <p className="text-xs text-slate-600">No deliveries yet.</p>}
                  {log.map((d, i) => (
                    <div key={i} className="flex items-center gap-2 text-xs">
                      <span className={d.ok ? 'text-emerald-400' : 'text-red-400'}>{d.ok ? '✓' : '✗'}</span>
                      <span className="text-slate-500">attempt {d.attempt || 'test'}</span>
                      <span className="text-slate-500">{d.status_code ?? (d.error || '—')}</span>
                      <span className="text-slate-600 ml-auto">{new Date(d.created_at).toLocaleString()}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>

        {editingId === 'new' || editingId ? (
          <div className={card}>
            <label className={label}>Event</label>
            <select value={form.event} onChange={e => setForm({ ...form, event: e.target.value })} className={field}>
              {events.map(ev => <option key={ev} value={ev} className="bg-neutral-900">{ev}</option>)}
            </select>

            <label className={`${label} mt-4`}>Your URL</label>
            <input value={form.url} onChange={e => setForm({ ...form, url: e.target.value })}
              placeholder="https://yoursite.com/webhooks/voiceagent" className={`${field} font-mono`} />

            <label className={`${label} mt-4`}>Signing secret</label>
            <input type="password" value={form.secret ?? ''}
              onChange={e => { setSecretTouched(true); setForm({ ...form, secret: e.target.value }) }}
              placeholder={editingId === 'new' ? 'Any string — you verify with this on your end' : 'Leave blank to keep the saved one'}
              className={`${field} font-mono`} />
            <p className="text-xs text-slate-500 mt-1.5">
              Every request carries an <code className="font-mono">X-Voiceagent-Signature</code> header — HMAC-SHA256
              of the raw body using this secret. Recompute it on your end and reject anything that
              doesn't match; that's what stops someone else pretending to be us.
            </p>

            <label className="flex items-center gap-2.5 text-sm text-slate-300 mt-4">
              <input type="checkbox" checked={form.enabled}
                onChange={e => setForm({ ...form, enabled: e.target.checked })} className="accent-violet-500" />
              Active
            </label>

            <div className="flex gap-2 pt-4">
              <button onClick={save} disabled={saving || !form.event || !form.url}
                className="px-5 py-2.5 rounded-xl bg-violet-600 hover:bg-violet-500 text-sm font-semibold disabled:opacity-40 transition-all">
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button onClick={() => setEditingId(null)}
                className="px-5 py-2.5 rounded-xl bg-white/6 hover:bg-white/12 text-sm text-slate-300 transition-all">
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button onClick={startNew}
            className="text-sm text-violet-400 hover:text-violet-300 font-medium">
            + Subscribe to an event
          </button>
        )}
      </main>
    </div>
  )
}
