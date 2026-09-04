import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  listTools, createTool, updateTool, deleteTool, testTool,
} from '../lib/api'
import type { BotTool, BotToolInput, ToolParameter } from '../lib/api'

// Task 3.1 — the form that makes a tool configuration rather than code.
//
// The API this page drives was the hard half; this is what turns it into
// something a customer can actually use. The manual's point about the generic
// HTTP tool applies here too: every field this form exposes is an integration
// nobody has to write code for, so it covers method, URL, headers, query,
// body, declared parameters and four kinds of authentication.

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const PARAM_TYPES: ToolParameter['type'][] = ['string', 'number', 'integer', 'boolean']

const AUTH_KINDS = [
  { value: 'none', label: 'None', hint: 'A public API, or one that needs no key.' },
  { value: 'bearer', label: 'Bearer token', hint: 'Sent as: Authorization: Bearer <key>' },
  { value: 'header', label: 'Custom header', hint: 'Sent as your own header, e.g. X-Api-Key.' },
  { value: 'query', label: 'Query parameter', hint: 'Appended to the URL, e.g. ?api_key=…' },
  { value: 'basic', label: 'Basic auth', hint: 'Enter it as username:password.' },
]

const BLANK: BotToolInput = {
  name: '', description: '', enabled: true, long_running: false, kind: 'http', builtin: '',
  method: 'GET', url: '', headers: {}, query: {}, body: {},
  parameters: [], auth: { kind: 'none', name: '', secret: '' },
  field_map: {}, timeout_seconds: 8,
}

/** Key/value maps are edited as rows so a customer never types JSON. */
type Pair = { k: string; v: string }
const toPairs = (o: Record<string, string>): Pair[] =>
  Object.entries(o || {}).map(([k, v]) => ({ k, v: String(v) }))
const fromPairs = (rows: Pair[]): Record<string, string> =>
  Object.fromEntries(rows.filter(r => r.k.trim()).map(r => [r.k.trim(), r.v]))

const card = 'bg-white/4 border border-white/8 rounded-2xl p-5'
const label = 'block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2'
const field = 'w-full bg-white/5 border border-white/10 rounded-xl px-3 py-2.5 text-sm text-white placeholder-slate-600 outline-none focus:border-violet-500/60 transition-all'
const smallField = 'flex-1 min-w-0 bg-white/5 border border-white/10 rounded-lg px-2.5 py-2 text-sm text-white placeholder-slate-600 outline-none focus:border-violet-500/60'

export default function BotToolsPage() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [tools, setTools] = useState<BotTool[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [editingId, setEditingId] = useState<string | null>(null)
  const [form, setForm] = useState<BotToolInput>(BLANK)
  const [headerRows, setHeaderRows] = useState<Pair[]>([])
  const [queryRows, setQueryRows] = useState<Pair[]>([])
  const [fieldMapRows, setFieldMapRows] = useState<Pair[]>([])
  const [secretTouched, setSecretTouched] = useState(false)
  const [saving, setSaving] = useState(false)

  const [testArgs, setTestArgs] = useState<Record<string, string>>({})
  const [testResult, setTestResult] = useState<string>('')
  const [testing, setTesting] = useState(false)

  useEffect(() => { refresh() }, [id])

  async function refresh() {
    try {
      setTools(await listTools(id!))
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  function startNew() {
    setEditingId('new'); setForm(BLANK)
    setHeaderRows([]); setQueryRows([]); setFieldMapRows([])
    setSecretTouched(false); setTestArgs({}); setTestResult('')
  }

  function startEdit(t: BotTool) {
    setEditingId(t.id)
    // The secret is deliberately absent: the API never returns it, and
    // leaving it out of the payload tells the server to keep the stored one.
    setForm({
      name: t.name, description: t.description, enabled: t.enabled,
      long_running: t.long_running, kind: t.kind,
      builtin: t.builtin, method: t.method, url: t.url, headers: t.headers,
      query: t.query, body: t.body, parameters: t.parameters,
      auth: { kind: t.auth.kind, name: t.auth.name },
      field_map: t.field_map, timeout_seconds: t.timeout_seconds,
    })
    setHeaderRows(toPairs(t.headers)); setQueryRows(toPairs(t.query))
    setFieldMapRows(toPairs(t.field_map))
    setSecretTouched(false); setTestArgs({}); setTestResult('')
  }

  function set<K extends keyof BotToolInput>(k: K, v: BotToolInput[K]) {
    setForm(prev => ({ ...prev, [k]: v }))
  }

  function setParam(i: number, patch: Partial<ToolParameter>) {
    setForm(prev => ({
      ...prev,
      parameters: prev.parameters.map((p, n) => (n === i ? { ...p, ...patch } : p)),
    }))
  }

  async function save() {
    setSaving(true); setError('')
    const payload: BotToolInput = {
      ...form,
      headers: fromPairs(headerRows),
      query: fromPairs(queryRows),
      field_map: fromPairs(fieldMapRows),
      auth: secretTouched
        ? form.auth
        : { kind: form.auth.kind, name: form.auth.name },   // omit `secret` → keep stored
    }
    try {
      if (editingId === 'new') await createTool(id!, payload)
      else await updateTool(id!, editingId!, payload)
      setEditingId(null)
      await refresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function remove(t: BotTool) {
    if (!confirm(`Delete “${t.name}”? The bot will stop being able to do this.`)) return
    try { await deleteTool(id!, t.id); await refresh() } catch (e: any) { setError(e.message) }
  }

  async function runTest() {
    if (editingId === 'new' || !editingId) return
    setTesting(true); setTestResult('')
    try {
      setTestResult(JSON.stringify(await testTool(id!, editingId, testArgs), null, 2))
    } catch (e: any) {
      setTestResult(`Could not run the test: ${e.message}`)
    } finally {
      setTesting(false)
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
        <button onClick={() => navigate(`/bots/${id}`)} className="p-1.5 text-slate-500 hover:text-white hover:bg-white/8 rounded-lg transition-all">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <line x1="19" y1="12" x2="5" y2="12" /><polyline points="12 19 5 12 12 5" />
          </svg>
        </button>
        <h1 className="font-bold">Tools</h1>
      </header>

      <main className="relative z-10 max-w-3xl mx-auto px-6 py-10 space-y-5">
        {error && (
          <div className="bg-red-500/10 text-red-400 border border-red-500/20 text-sm rounded-xl px-4 py-3">{error}</div>
        )}

        <div className={card}>
          <p className="text-sm text-slate-400">
            Tools are what let this bot <em className="text-slate-300 not-italic font-medium">do</em> things —
            look up an order, check stock, book a slot — instead of only talking.
            Each one describes a call to your own system, and the bot decides when to use it
            from the description you write.
          </p>
          {tools.length === 0 && (
            <p className="text-sm text-slate-500 mt-3">
              This bot has none configured, so it uses the three built-in demonstration tools.
              Adding one here replaces those with your own.
            </p>
          )}
        </div>

        {/* The list */}
        <div className="space-y-2">
          {tools.map(t => (
            <div key={t.id} className={`${card} flex items-start gap-3`}>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-sm text-violet-300">{t.name}</span>
                  <span className="text-[11px] font-mono px-1.5 py-0.5 rounded bg-white/8 text-slate-400">
                    {t.kind === 'builtin' ? 'built in' : `${t.method}`}
                  </span>
                  {!t.enabled && (
                    <span className="text-[11px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-400">off</span>
                  )}
                  {t.auth.has_secret && (
                    <span className="text-[11px] font-mono px-1.5 py-0.5 rounded bg-emerald-500/12 text-emerald-400">
                      key {t.auth.secret_masked}
                    </span>
                  )}
                </div>
                <p className="text-sm text-slate-400 mt-1">{t.description}</p>
                {t.kind === 'http' && (
                  <p className="text-xs font-mono text-slate-600 mt-1 truncate">{t.url}</p>
                )}
              </div>
              <div className="flex gap-1.5 shrink-0">
                <button onClick={() => startEdit(t)} className="text-xs px-3 py-1.5 rounded-lg bg-white/6 hover:bg-white/12 text-slate-300 transition-all">Edit</button>
                <button onClick={() => remove(t)} className="text-xs px-3 py-1.5 rounded-lg text-red-400/80 hover:bg-red-500/12 transition-all">Delete</button>
              </div>
            </div>
          ))}
        </div>

        {editingId === null && (
          <button onClick={startNew} className="w-full py-3 rounded-2xl border border-dashed border-white/15 text-sm text-slate-400 hover:border-violet-500/50 hover:text-white transition-all">
            + Add a tool
          </button>
        )}

        {/* The editor */}
        {editingId !== null && (
          <div className={`${card} space-y-5`}>
            <h2 className="font-bold">{editingId === 'new' ? 'New tool' : 'Edit tool'}</h2>

            <div>
              <label className={label}>Name the AI will use</label>
              <input value={form.name} onChange={e => set('name', e.target.value)}
                placeholder="check_stock" className={`${field} font-mono`} />
              <p className="text-xs text-slate-500 mt-1.5">Letters, numbers and underscores — it becomes a function name.</p>
            </div>

            <div>
              <label className={label}>When should the bot use this?</label>
              <textarea rows={2} value={form.description} onChange={e => set('description', e.target.value)}
                placeholder="Check whether an item is in stock, given its SKU code." className={field} />
              <p className="text-xs text-slate-500 mt-1.5">
                This is the only thing the AI reads when deciding. Be specific — it matters more than it looks.
              </p>
            </div>

            <div className="flex gap-3">
              <div className="w-32">
                <label className={label}>Method</label>
                <select value={form.method} onChange={e => set('method', e.target.value)} className={field}>
                  {METHODS.map(m => <option key={m} value={m} className="bg-neutral-900">{m}</option>)}
                </select>
              </div>
              <div className="flex-1 min-w-0">
                <label className={label}>URL</label>
                <input value={form.url} onChange={e => set('url', e.target.value)}
                  placeholder="https://api.yourshop.com/stock/{sku}" className={`${field} font-mono text-xs`} />
              </div>
            </div>
            <p className="text-xs text-slate-500 -mt-3">
              Put <span className="font-mono text-slate-400">{'{braces}'}</span> where a value should go. Anything in braces
              must be listed as an input below, and the AI fills it in from the conversation.
            </p>

            {/* Parameters */}
            <div>
              <label className={label}>Inputs the AI must work out</label>
              <div className="space-y-2">
                {form.parameters.map((p, i) => (
                  <div key={i} className="flex gap-2 items-start">
                    <input value={p.name} onChange={e => setParam(i, { name: e.target.value })}
                      placeholder="sku" className={`${smallField} font-mono max-w-[130px]`} />
                    <select value={p.type} onChange={e => setParam(i, { type: e.target.value as ToolParameter['type'] })}
                      className={`${smallField} max-w-[110px]`}>
                      {PARAM_TYPES.map(t => <option key={t} value={t} className="bg-neutral-900">{t}</option>)}
                    </select>
                    <input value={p.description} onChange={e => setParam(i, { description: e.target.value })}
                      placeholder="The item's SKU code" className={smallField} />
                    <label className="flex items-center gap-1.5 text-xs text-slate-400 py-2 shrink-0">
                      <input type="checkbox" checked={p.required} onChange={e => setParam(i, { required: e.target.checked })}
                        className="accent-violet-500" />
                      required
                    </label>
                    <button onClick={() => set('parameters', form.parameters.filter((_, n) => n !== i))}
                      className="text-slate-600 hover:text-red-400 px-1 py-2 shrink-0">×</button>
                  </div>
                ))}
              </div>
              <button
                onClick={() => set('parameters', [...form.parameters, { name: '', type: 'string', description: '', required: true }])}
                className="text-xs text-violet-400 hover:text-violet-300 mt-2">+ Add an input</button>
            </div>

            <PairEditor title="Headers" rows={headerRows} onChange={setHeaderRows} placeholderKey="Content-Type" placeholderValue="application/json" />
            <PairEditor title="Query parameters" rows={queryRows} onChange={setQueryRows} placeholderKey="format" placeholderValue="json" />

            {/* Auth */}
            <div>
              <label className={label}>Authentication</label>
              <select value={form.auth.kind}
                onChange={e => set('auth', { ...form.auth, kind: e.target.value })}
                className={field}>
                {AUTH_KINDS.map(a => <option key={a.value} value={a.value} className="bg-neutral-900">{a.label}</option>)}
              </select>
              <p className="text-xs text-slate-500 mt-1.5">
                {AUTH_KINDS.find(a => a.value === form.auth.kind)?.hint}
              </p>

              {(form.auth.kind === 'header' || form.auth.kind === 'query') && (
                <input value={form.auth.name}
                  onChange={e => set('auth', { ...form.auth, name: e.target.value })}
                  placeholder={form.auth.kind === 'header' ? 'X-Api-Key' : 'api_key'}
                  className={`${field} font-mono mt-2`} />
              )}

              {form.auth.kind !== 'none' && (
                <>
                  <input type="password" value={form.auth.secret ?? ''}
                    onChange={e => { setSecretTouched(true); set('auth', { ...form.auth, secret: e.target.value }) }}
                    placeholder={editingId === 'new' ? 'Your API key' : 'Leave blank to keep the saved key'}
                    className={`${field} font-mono mt-2`} />
                  <p className="text-xs text-slate-500 mt-1.5">
                    Stored encrypted. It is never shown again — only the last four characters.
                  </p>
                </>
              )}
            </div>

            {/* Task 3.6 — the lookup template */}
            <div>
              <label className={label}>Rename fields for the bot (optional)</label>
              <p className="text-xs text-slate-500 mb-2.5">
                If the response is deeply nested, give the bot a plain name for the part it
                needs — e.g. the field name <code className="font-mono">status</code> could point
                to <code className="font-mono">data.order.delivery_status</code>. Leave empty to
                just hand the bot the whole response as-is.
              </p>
              <PairEditor rows={fieldMapRows} onChange={setFieldMapRows}
                placeholderKey="status" placeholderValue="data.order.delivery_status" />
            </div>

            <div>
              <label className={label}>Give up after (seconds)</label>
              <input type="number" min={1} max={30} step={0.5}
                value={form.timeout_seconds}
                onChange={e => set('timeout_seconds', Number(e.target.value) || 8)}
                className={`${field} max-w-[140px]`} />
              <p className="text-xs text-slate-500 mt-1.5">
                For a quick lookup, around 3 seconds is best — it's far better for the bot to say
                "that system isn't responding" than to leave the caller waiting in silence.
                Slower actions (like a booking) may need longer.
              </p>
            </div>

            <div className="space-y-3">
              <label className="flex items-center gap-2.5 text-sm text-slate-300">
                <input type="checkbox" checked={form.enabled}
                  onChange={e => set('enabled', e.target.checked)} className="accent-violet-500" />
                Available to the bot
              </label>

              {/* Task 3.3 */}
              <div>
                <label className="flex items-center gap-2.5 text-sm text-slate-300">
                  <input type="checkbox" checked={form.long_running}
                    onChange={e => set('long_running', e.target.checked)} className="accent-violet-500" />
                  This one is slow
                </label>
                <p className="text-xs text-slate-500 mt-1 ml-6">
                  The bot will say it is working on it and keep talking, then tell the caller
                  the answer when it arrives — instead of leaving them in silence.
                  Turn this on if the system usually takes more than a few seconds.
                </p>
              </div>
            </div>

            {/* Test */}
            {editingId !== 'new' && (
              <div className="border-t border-white/8 pt-4">
                <label className={label}>Try it now</label>
                <p className="text-xs text-slate-500 mb-2.5">
                  Runs the tool once against your real system, so a wrong URL or key turns up here
                  instead of in the middle of a phone call.
                </p>
                <div className="space-y-2">
                  {form.parameters.filter(p => p.name).map(p => (
                    <div key={p.name} className="flex gap-2 items-center">
                      <span className="font-mono text-xs text-slate-500 w-28 shrink-0 truncate">{p.name}</span>
                      <input value={testArgs[p.name] ?? ''}
                        onChange={e => setTestArgs({ ...testArgs, [p.name]: e.target.value })}
                        placeholder="a real value to try" className={smallField} />
                    </div>
                  ))}
                </div>
                <button onClick={runTest} disabled={testing}
                  className="mt-3 text-sm px-4 py-2 rounded-xl bg-white/8 hover:bg-white/14 text-white disabled:opacity-50 transition-all">
                  {testing ? 'Running…' : 'Run test'}
                </button>
                {testResult && (
                  <pre className="mt-3 text-xs font-mono bg-black/40 border border-white/8 rounded-xl p-3 overflow-x-auto text-slate-300 whitespace-pre-wrap">
                    {testResult}
                  </pre>
                )}
              </div>
            )}

            <div className="flex gap-2 pt-1">
              <button onClick={save} disabled={saving || !form.name || !form.description}
                className="px-5 py-2.5 rounded-xl bg-violet-600 hover:bg-violet-500 text-sm font-semibold disabled:opacity-40 transition-all">
                {saving ? 'Saving…' : 'Save tool'}
              </button>
              <button onClick={() => { setEditingId(null); setError('') }}
                className="px-5 py-2.5 rounded-xl bg-white/6 hover:bg-white/12 text-sm text-slate-300 transition-all">
                Cancel
              </button>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}

/** Headers, query parameters and field mappings — edited as rows so nobody
 *  has to type JSON. `title` is omitted where the caller already put a
 *  fuller explanation in its own label just above. */
function PairEditor({ title, rows, onChange, placeholderKey, placeholderValue }: {
  title?: string
  rows: Pair[]
  onChange: (r: Pair[]) => void
  placeholderKey: string
  placeholderValue: string
}) {
  return (
    <div>
      {title && <label className={label}>{title}</label>}
      <div className="space-y-2">
        {rows.map((r, i) => (
          <div key={i} className="flex gap-2">
            <input value={r.k} placeholder={placeholderKey} className={`${smallField} font-mono max-w-[190px]`}
              onChange={e => onChange(rows.map((x, n) => (n === i ? { ...x, k: e.target.value } : x)))} />
            <input value={r.v} placeholder={placeholderValue} className={`${smallField} font-mono`}
              onChange={e => onChange(rows.map((x, n) => (n === i ? { ...x, v: e.target.value } : x)))} />
            <button onClick={() => onChange(rows.filter((_, n) => n !== i))}
              className="text-slate-600 hover:text-red-400 px-1 shrink-0">×</button>
          </div>
        ))}
      </div>
      <button onClick={() => onChange([...rows, { k: '', v: '' }])}
        className="text-xs text-violet-400 hover:text-violet-300 mt-2">+ Add</button>
    </div>
  )
}
