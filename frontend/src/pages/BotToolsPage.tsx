import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  listTools, createTool, updateTool, deleteTool, testTool,
} from '../lib/api'
import { usePageChrome } from '../context/ChromeContext'
import PageLoader from '../components/PageLoader'
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
  payment: {
    enabled: false, reference_field: '', amount_field: '', link_field: '',
    signature_header: 'X-Razorpay-Signature',
    webhook_reference_field: 'payload.payment_link.entity.id',
    webhook_status_field: 'payload.payment_link.entity.status',
    webhook_paid_value: 'paid', webhook_secret: '',
  },
  approval: { enabled: false, amount_parameter: 'amount', threshold: 0 },
  undo: { url: '', method: 'DELETE', headers: {}, body: {} },
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

  usePageChrome('Tools', `/bots/${id}`)

  const [tools, setTools] = useState<BotTool[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [editingId, setEditingId] = useState<string | null>(null)
  const [form, setForm] = useState<BotToolInput>(BLANK)
  const [headerRows, setHeaderRows] = useState<Pair[]>([])
  const [queryRows, setQueryRows] = useState<Pair[]>([])
  const [fieldMapRows, setFieldMapRows] = useState<Pair[]>([])
  const [undoHeaderRows, setUndoHeaderRows] = useState<Pair[]>([])
  const [secretTouched, setSecretTouched] = useState(false)
  const [paymentSecretTouched, setPaymentSecretTouched] = useState(false)
  // The request body is edited as raw JSON text, not as key/value rows like
  // headers and query are. Those are flat maps of strings; a real request
  // body is neither — Razorpay's payment-link call alone needs a nested
  // object (`customer: {name, contact}`) and real booleans
  // (`accept_partial: false`), and rows cannot express either. Kept as TEXT
  // in its own state rather than parsed on every keystroke so a
  // half-finished edit doesn't get thrown away mid-typing.
  const [bodyText, setBodyText] = useState('')
  const [bodyError, setBodyError] = useState('')
  const [copiedWebhook, setCopiedWebhook] = useState(false)
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
    setHeaderRows([]); setQueryRows([]); setFieldMapRows([]); setUndoHeaderRows([])
    setSecretTouched(false); setPaymentSecretTouched(false)
    setBodyText(''); setBodyError('')
    setTestArgs({}); setTestResult('')
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
      // webhook_secret deliberately absent — same rule as the API key: the
      // server never returns it, and omitting it means "keep the stored one".
      payment: { ...t.payment, webhook_secret: undefined },
      approval: t.approval,
      undo: t.undo,
    })
    setHeaderRows(toPairs(t.headers)); setQueryRows(toPairs(t.query))
    setFieldMapRows(toPairs(t.field_map)); setUndoHeaderRows(toPairs(t.undo.headers))
    setSecretTouched(false); setPaymentSecretTouched(false)
    // Pretty-printed rather than compact: this is the one field a customer
    // reads back to check, and a one-line blob of JSON is unreadable.
    setBodyText(t.body && Object.keys(t.body).length ? JSON.stringify(t.body, null, 2) : '')
    setBodyError('')
    setTestArgs({}); setTestResult('')
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
    // Parsed HERE rather than on every keystroke, and a failure stops the
    // save outright. Saving anyway would silently store an empty body while
    // the textarea still shows the customer's JSON — they would then watch a
    // POST go out with nothing in it and have no way to tell why.
    let parsedBody: Record<string, unknown> = {}
    if (bodyText.trim()) {
      try {
        const parsed = JSON.parse(bodyText)
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          setBodyError('The body must be a JSON object — it starts with { and ends with }.')
          return
        }
        parsedBody = parsed
      } catch (e: any) {
        setBodyError(`That is not valid JSON: ${e.message}`)
        return
      }
    }
    setBodyError('')

    setSaving(true); setError('')
    const payload: BotToolInput = {
      ...form,
      body: parsedBody,
      headers: fromPairs(headerRows),
      query: fromPairs(queryRows),
      field_map: fromPairs(fieldMapRows),
      undo: { ...form.undo, headers: fromPairs(undoHeaderRows) },
      payment: paymentSecretTouched
        ? form.payment
        : { ...form.payment, webhook_secret: undefined },
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

  if (loading) return <PageLoader />

  return (
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
              <input autoComplete="off" value={form.name} onChange={e => set('name', e.target.value)}
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
                <input autoComplete="off" value={form.url} onChange={e => set('url', e.target.value)}
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
                    <input autoComplete="off" value={p.name} onChange={e => setParam(i, { name: e.target.value })}
                      placeholder="sku" className={`${smallField} font-mono max-w-[130px]`} />
                    <select value={p.type} onChange={e => setParam(i, { type: e.target.value as ToolParameter['type'] })}
                      className={`${smallField} max-w-[110px]`}>
                      {PARAM_TYPES.map(t => <option key={t} value={t} className="bg-neutral-900">{t}</option>)}
                    </select>
                    <input autoComplete="off" value={p.description} onChange={e => setParam(i, { description: e.target.value })}
                      placeholder="The item's SKU code" className={smallField} />
                    <label className="flex items-center gap-1.5 text-xs text-slate-400 py-2 shrink-0">
                      <input autoComplete="off" type="checkbox" checked={p.required} onChange={e => setParam(i, { required: e.target.checked })}
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

            {/* Request body. Only for methods that actually carry one — a GET
                with a body is meaningless here (the server sends it as
                `json=` only when non-empty) and showing the field would
                invite someone to fill it in and wonder why nothing happened. */}
            {['POST', 'PUT', 'PATCH'].includes(form.method) && (
              <div>
                <label className={label}>Body (JSON)</label>
                <p className="text-xs text-slate-500 mb-2">
                  Sent as the request body. Use <code className="text-slate-400">{'{name}'}</code> to
                  drop in one of the inputs above — it works anywhere in here, including inside
                  nested objects.
                </p>
                <textarea
                  rows={10}
                  spellCheck={false}
                  value={bodyText}
                  onChange={e => { setBodyText(e.target.value); if (bodyError) setBodyError('') }}
                  placeholder={'{\n  "amount": "{amount}",\n  "currency": "INR"\n}'}
                  className={`${field} font-mono text-xs leading-relaxed resize-y ${
                    bodyError ? 'border-red-500/60 focus:border-red-500/60' : ''
                  }`}
                />
                {bodyError && (
                  <p className="text-xs text-red-400 mt-1.5">{bodyError}</p>
                )}
                <p className="text-xs text-slate-600 mt-1.5">
                  Leave empty to send no body. Numbers and true/false stay as they are — only
                  text in quotes has placeholders filled in.
                </p>
              </div>
            )}

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
                <input autoComplete="off" value={form.auth.name}
                  onChange={e => set('auth', { ...form.auth, name: e.target.value })}
                  placeholder={form.auth.kind === 'header' ? 'X-Api-Key' : 'api_key'}
                  className={`${field} font-mono mt-2`} />
              )}

              {form.auth.kind !== 'none' && (
                <>
                  <input autoComplete="off" type="password" value={form.auth.secret ?? ''}
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
              <input autoComplete="off" type="number" min={1} max={30} step={0.5}
                value={form.timeout_seconds}
                onChange={e => set('timeout_seconds', Number(e.target.value) || 8)}
                className={`${field} max-w-[140px]`} />
              <p className="text-xs text-slate-500 mt-1.5">
                For a quick lookup, around 3 seconds is best — it's far better for the bot to say
                "that system isn't responding" than to leave the caller waiting in silence.
                Slower actions (like a booking) may need longer.
              </p>
            </div>

            {/* Task 3.7 — the payment link tool */}
            <div className="border border-white/8 rounded-xl p-4">
              <label className="flex items-center gap-2.5 text-sm text-slate-300">
                <input autoComplete="off" type="checkbox" checked={form.payment.enabled}
                  onChange={e => set('payment', { ...form.payment, enabled: e.target.checked })}
                  className="accent-violet-500" />
                This tool creates a payment link
              </label>
              <p className="text-xs text-slate-500 mt-1 ml-6">
                The bot will be told never to take card details by voice, and the caller is
                told automatically when the payment arrives — even mid-sentence.
              </p>

              {form.payment.enabled && (
                <div className="mt-4 space-y-3 ml-6">
                  <p className="text-xs text-slate-400">
                    Where to find each thing in the payment provider's reply:
                  </p>
                  {([
                    ['reference_field', 'Payment reference', 'id'],
                    ['link_field', 'The link itself', 'short_url'],
                    ['amount_field', 'Amount (optional)', 'amount'],
                  ] as const).map(([key, labelText, ph]) => (
                    <div key={key} className="flex gap-2 items-center">
                      <span className="text-xs text-slate-500 w-40 shrink-0">{labelText}</span>
                      <input autoComplete="off" value={form.payment[key]} placeholder={ph}
                        onChange={e => set('payment', { ...form.payment, [key]: e.target.value })}
                        className={`${smallField} font-mono`} />
                    </div>
                  ))}

                  <p className="text-xs text-slate-400 pt-2">
                    And how the provider tells us it was paid:
                  </p>
                  {([
                    ['signature_header', 'Signature header', 'X-Razorpay-Signature'],
                    ['webhook_reference_field', 'Reference in webhook', 'payload.payment_link.entity.id'],
                    ['webhook_status_field', 'Status in webhook', 'payload.payment_link.entity.status'],
                    ['webhook_paid_value', 'Value meaning "paid"', 'paid'],
                  ] as const).map(([key, labelText, ph]) => (
                    <div key={key} className="flex gap-2 items-center">
                      <span className="text-xs text-slate-500 w-40 shrink-0">{labelText}</span>
                      <input autoComplete="off" value={form.payment[key]} placeholder={ph}
                        onChange={e => set('payment', { ...form.payment, [key]: e.target.value })}
                        className={`${smallField} font-mono`} />
                    </div>
                  ))}

                  <div className="flex gap-2 items-center">
                    <span className="text-xs text-slate-500 w-40 shrink-0">Webhook secret</span>
                    <input autoComplete="off" type="password" value={form.payment.webhook_secret ?? ''}
                      onChange={e => {
                        setPaymentSecretTouched(true)
                        set('payment', { ...form.payment, webhook_secret: e.target.value })
                      }}
                      placeholder={editingId === 'new' ? 'From your provider dashboard' : 'Leave blank to keep the saved one'}
                      className={`${smallField} font-mono`} />
                  </div>
                  <p className="text-xs text-slate-500">
                    Without a matching secret every callback is rejected — that is what stops
                    anyone else claiming a payment succeeded.
                  </p>

                  {/* The full absolute URL, not just the path. This gets pasted
                      into somebody else's dashboard, where a relative path is
                      useless — and reconstructing the origin by hand is exactly
                      the kind of step that gets one character wrong and then
                      fails as a webhook that silently never arrives. */}
                  <div className="mt-1">
                    <span className="text-xs text-slate-500">Point your provider's webhook at</span>
                    {editingId === 'new' ? (
                      <p className="text-xs text-slate-600 mt-1">
                        Save the tool first — the address includes its ID, which does not exist yet.
                      </p>
                    ) : (
                      <div className="flex gap-2 items-center mt-1">
                        <code className="flex-1 min-w-0 truncate bg-black/30 border border-white/10 rounded-lg px-2.5 py-2 font-mono text-xs text-slate-300">
                          {`${window.location.origin}/payments/webhook/${editingId}`}
                        </code>
                        <button
                          type="button"
                          onClick={() => {
                            navigator.clipboard
                              ?.writeText(`${window.location.origin}/payments/webhook/${editingId}`)
                              .then(() => { setCopiedWebhook(true); setTimeout(() => setCopiedWebhook(false), 1800) })
                              .catch(() => {})
                          }}
                          className="shrink-0 text-xs px-2.5 py-2 rounded-lg border border-white/10 text-slate-400 hover:text-white hover:bg-white/5 transition-all"
                        >
                          {copiedWebhook ? 'Copied' : 'Copy'}
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* Task 3.4 — how this tool takes itself back */}
            <div className="border border-white/8 rounded-xl p-4">
              <label className={label}>Undoing this</label>
              <p className="text-xs text-slate-500 mb-3">
                If the caller asks for several things at once and a later one fails, this tool
                is taken back automatically — but only if you say how here. Leave it empty for
                anything that cannot be undone (a sent message, a charged card); the caller is
                then told plainly that it still stands.
              </p>
              <div className="flex gap-2">
                <select value={form.undo.method}
                  onChange={e => set('undo', { ...form.undo, method: e.target.value })}
                  className={`${field} max-w-[120px]`}>
                  {METHODS.map(m => <option key={m} value={m} className="bg-[#12121f]">{m}</option>)}
                </select>
                <input autoComplete="off" value={form.undo.url}
                  onChange={e => set('undo', { ...form.undo, url: e.target.value })}
                  placeholder="https://your-system.com/bookings/{booking_id}" className={`${field} font-mono`} />
              </div>
              <p className="text-xs text-slate-500 mt-2">
                It runs with the same values the original call used, so {'{booking_id}'} here
                means the same thing it did above.
              </p>
              {form.undo.url && (
                <div className="mt-4">
                  <p className="text-xs text-slate-500 mb-2">
                    Extra headers for the undo call. Leave empty to reuse the tool's own headers
                    and key.
                  </p>
                  <PairEditor rows={undoHeaderRows} onChange={setUndoHeaderRows}
                    placeholderKey="Header" placeholderValue="value" />
                </div>
              )}
            </div>

            {/* Task 3.10 — human approval for big actions */}
            <div className="border border-white/8 rounded-xl p-4">
              <label className="flex items-center gap-2.5 text-sm text-slate-300">
                <input autoComplete="off" type="checkbox" checked={form.approval.enabled}
                  onChange={e => set('approval', { ...form.approval, enabled: e.target.checked })}
                  className="accent-violet-500" />
                Require a person's approval above a value
              </label>
              <p className="text-xs text-slate-500 mt-1 ml-6">
                Above the threshold, this tool sends the request for a person to approve instead
                of running it — nothing happens until someone says yes on the Approvals page.
              </p>

              {form.approval.enabled && (
                <div className="mt-4 space-y-3 ml-6">
                  <div className="flex gap-2 items-center">
                    <span className="text-xs text-slate-500 w-32 shrink-0">Amount is in input</span>
                    <input autoComplete="off" value={form.approval.amount_parameter}
                      onChange={e => set('approval', { ...form.approval, amount_parameter: e.target.value })}
                      placeholder="amount" className={`${smallField} font-mono`} />
                  </div>
                  <div className="flex gap-2 items-center">
                    <span className="text-xs text-slate-500 w-32 shrink-0">Approve automatically up to</span>
                    <input autoComplete="off" type="number" min={0} step="any" value={form.approval.threshold}
                      onChange={e => set('approval', { ...form.approval, threshold: Number(e.target.value) || 0 })}
                      className={`${smallField} font-mono max-w-[140px]`} />
                  </div>
                  <p className="text-xs text-slate-500">
                    Set this to whatever a small shop or a bank would want differently — there's
                    no right number, only the one that matches your own comfort with letting the
                    bot act on its own.
                  </p>
                </div>
              )}
            </div>

            <div className="space-y-3">
              <label className="flex items-center gap-2.5 text-sm text-slate-300">
                <input autoComplete="off" type="checkbox" checked={form.enabled}
                  onChange={e => set('enabled', e.target.checked)} className="accent-violet-500" />
                Available to the bot
              </label>

              {/* Task 3.3 */}
              <div>
                <label className="flex items-center gap-2.5 text-sm text-slate-300">
                  <input autoComplete="off" type="checkbox" checked={form.long_running}
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
                      <input autoComplete="off" value={testArgs[p.name] ?? ''}
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
            <input autoComplete="off" value={r.k} placeholder={placeholderKey} className={`${smallField} font-mono max-w-[190px]`}
              onChange={e => onChange(rows.map((x, n) => (n === i ? { ...x, k: e.target.value } : x)))} />
            <input autoComplete="off" value={r.v} placeholder={placeholderValue} className={`${smallField} font-mono`}
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
