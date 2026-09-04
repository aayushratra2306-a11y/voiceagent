import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  createBot, updateBot, listBots, listDocuments, uploadDocument, deleteDocument,
  listBotTemplates, createTool,
} from '../lib/api'
import type { Bot, BotDocument, BotTemplate } from '../lib/api'

// Voices per language. Until 2026-09-04 this was three English voices shown
// to every bot whatever its language, so a Hindi bot was necessarily given
// an English voice and Cartesia — which is multilingual — read Hindi words
// with an English accent. Voices are now native to the language selected.
//
// The English three are unchanged in id (existing bots point at them) but
// their labels were simply wrong: 'Atlas — Professional, confident male
// voice' is really Sierra, a Californian woman, and 'Aria — Neutral' is
// really Greg, a man. Verified against Cartesia's API; names are the real
// ones now.
//
// Must stay in sync with VOICES in backend/app/pipeline/language.py, which
// applies the same mapping server-side for bots saved before this existed.
const VOICES: Record<string, { id: string; label: string; desc: string }[]> = {
  en: [
    { id: 'a0e99841-438c-4a64-b679-ae501e7d6091', label: 'Greg — Supportive', desc: 'Clear, balanced male voice' },
    { id: '694f9389-aac1-45b6-b726-9d9369183238', label: 'Sarah — Mindful', desc: 'Warm female voice' },
    { id: 'b7d50908-b17c-442d-ad8d-810c63997ed9', label: 'Sierra — Bright', desc: 'Upbeat female voice' },
  ],
  hi: [
    { id: '6b02ffe5-e3cb-48c0-a023-c72f85953375', label: 'Sneha — Empathetic', desc: 'Gentle, reassuring female voice' },
    { id: 'adf97b9d-905c-41de-9fe9-afb387116d06', label: 'Vikas — Approachable', desc: 'Polite, friendly male voice' },
  ],
  fr: [
    { id: 'e2ab5462-e7c8-492d-a244-41f39444af6e', label: 'Audrey — Customer Service', desc: 'Clear, attentive female voice' },
    { id: 'cc4276e6-1ebc-429a-8c7d-930993d51abc', label: 'Julien — Polished', desc: 'Professional, warm male voice' },
  ],
  de: [
    { id: '38aabb6a-f52b-4fb0-a3d1-988518f4dc06', label: 'Alina — Engaging', desc: 'Warm female voice for assistants' },
    { id: 'e00dd3df-19e7-4cd4-827a-7ff6687b6954', label: 'Lukas — Professional', desc: 'Confident male voice' },
  ],
  es: [
    { id: 'de38f545-c574-44e8-9b54-a7d6fec1c6b1', label: 'Marta — Friendly Guide', desc: 'Approachable female voice' },
    { id: 'b0689631-eee7-4a6c-bb86-195f1d267c2e', label: 'Emilio — Optimistic', desc: 'Upbeat male voice' },
  ],
}

const voicesFor = (lang: string) => VOICES[lang] ?? VOICES.en

const LANGUAGES = [
  { code: 'en', label: 'English', flag: '🇺🇸' },
  { code: 'hi', label: 'Hindi', flag: '🇮🇳' },
  { code: 'es', label: 'Spanish', flag: '🇪🇸' },
  { code: 'fr', label: 'French', flag: '🇫🇷' },
  { code: 'de', label: 'German', flag: '🇩🇪' },
]

const MODELS = [
  { id: 'gpt-4o-mini', label: 'GPT-4o Mini', desc: 'Fast · Free tier' },
  { id: 'gpt-4o', label: 'GPT-4o', desc: 'Smarter · Paid' },
]

const DEFAULTS = {
  name: '',
  system_prompt: 'You are a helpful voice assistant.',
  voice_id: VOICES.en[0].id,
  llm_model: 'gpt-4o-mini',
  language: 'en',
}

export default function BotSettingsPage() {
  const { id } = useParams()
  const isNew = id === 'new'
  const navigate = useNavigate()
  const [form, setForm] = useState<Omit<Bot, 'id'>>(DEFAULTS)
  const [loading, setLoading] = useState(!isNew)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [docs, setDocs] = useState<BotDocument[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Task 3.9 — starting points instead of a blank instruction box. Only
  // fetched for a brand new bot; an existing one is already past this.
  const [templates, setTemplates] = useState<BotTemplate[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null)

  useEffect(() => {
    if (isNew) listBotTemplates().then(setTemplates).catch(() => {})
  }, [isNew])

  function applyTemplate(t: BotTemplate) {
    setSelectedTemplateId(t.id)
    // Only the prompt — name is the customer's own to choose, and voice/
    // language aren't part of what a template is curating. "Edit freely
    // afterwards" (the manual's own fourth step) means this is a starting
    // point, not a lock: every field below stays fully editable.
    setForm(prev => ({ ...prev, system_prompt: t.system_prompt }))
  }

  useEffect(() => {
    if (isNew) return
    listBots().then(bots => {
      const bot = bots.find(b => b.id === id)
      // Bots saved before voices were per-language hold a voice from the old
      // English-only list, so their stored voice_id is not among the ones now
      // offered for their language and NO radio would appear selected —
      // making the form look broken and letting a save write the mismatch
      // straight back. Show the language's default instead, which is also
      // what the server substitutes at call time (language.resolve_voice), so
      // the page agrees with what the caller actually hears.
      if (bot) {
        const allowed = voicesFor(bot.language)
        const voice_id = allowed.some(v => v.id === bot.voice_id)
          ? bot.voice_id
          : allowed[0].id
        setForm({ name: bot.name, system_prompt: bot.system_prompt, voice_id, llm_model: bot.llm_model, language: bot.language })
      }
      setLoading(false)
    })
    listDocuments(id!).then(setDocs).catch(() => {})
  }, [id])

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file || isNew) return
    setUploading(true)
    setUploadError('')
    try {
      const doc = await uploadDocument(id!, file)
      setDocs(prev => [...prev, doc])
    } catch (err: any) {
      setUploadError(err.message)
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  async function handleDeleteDoc(docId: string) {
    try {
      await deleteDocument(docId)
      setDocs(prev => prev.filter(d => d.id !== docId))
    } catch (err: any) {
      setUploadError(`Delete failed: ${err.message}`)
    }
  }

  function set(field: keyof typeof form, value: string) {
    setForm(prev => ({ ...prev, [field]: value }))
  }

  // Changing language has to move the voice with it, otherwise the form
  // keeps a voice that speaks the old language and the radio list shows
  // nothing selected — the state that produced Hindi in an English accent.
  // Kept in one updater rather than two set() calls so language and voice
  // can never be briefly inconsistent with each other.
  function setLanguage(value: string) {
    setForm(prev => {
      const allowed = voicesFor(value)
      const keep = allowed.some(v => v.id === prev.voice_id)
      return { ...prev, language: value, voice_id: keep ? prev.voice_id : allowed[0].id }
    })
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    try {
      if (isNew) {
        const created = await createBot(form)
        // Task 3.9's "sensible tool selections" — a template names builtin
        // tools by their function name; each becomes one BotTool(kind=
        // "builtin") row, exactly like any other tool a customer could
        // configure by hand (see BotToolsPage). A bot with these rows gets
        // ONLY these tools rather than every builtin there is (task 3.1's
        // fallback for a bot with nothing configured) — a Tutor template
        // offered book_appointment would be exactly the irrelevant-tool
        // problem 3.1 exists to get away from.
        const template = templates.find(t => t.id === selectedTemplateId)
        if (template) {
          await Promise.all(template.tools.map(toolName => createTool(created.id, {
            name: toolName, description: `Template default: ${toolName}`,
            enabled: true, long_running: false, kind: 'builtin', builtin: toolName,
            method: 'GET', url: '', headers: {}, query: {}, body: {},
            parameters: [], auth: { kind: 'none', name: '' },
            field_map: {}, timeout_seconds: 8,
            payment: {
              enabled: false, reference_field: '', amount_field: '', link_field: '',
              signature_header: 'X-Razorpay-Signature',
              webhook_reference_field: 'payload.payment_link.entity.id',
              webhook_status_field: 'payload.payment_link.entity.status',
              webhook_paid_value: 'paid',
            },
            approval: { enabled: false, amount_parameter: 'amount', threshold: 0 },
          })))
        }
      } else {
        await updateBot(id!, form)
      }
      navigate('/dashboard')
    } catch (err: any) {
      setError(err.message)
    } finally {
      setSaving(false)
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
      <div className="absolute bottom-[-20%] left-[-5%] w-[400px] h-[400px] rounded-full bg-indigo-700/15 blur-[120px] pointer-events-none" />

      <header className="relative z-10 border-b border-white/8 px-6 py-4 flex items-center gap-3 backdrop-blur-sm">
        <button onClick={() => navigate('/dashboard')} className="p-1.5 text-slate-500 hover:text-white hover:bg-white/8 rounded-lg transition-all">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
        </button>
        <h1 className="font-bold text-white">{isNew ? 'New Bot' : 'Edit Bot'}</h1>
      </header>

      <main className="relative z-10 max-w-xl mx-auto px-6 py-10">
        <form onSubmit={handleSubmit} className="space-y-5">
          {error && (
            <div className="bg-red-500/10 text-red-400 border border-red-500/20 text-sm rounded-xl px-4 py-3">
              {error}
            </div>
          )}

          {/* Task 3.9 — starting points instead of a blank instruction box */}
          {isNew && templates.length > 0 && (
            <div className="bg-white/4 border border-white/8 rounded-2xl p-5">
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">
                Start from a template
              </label>
              <p className="text-xs text-slate-500 mb-3">
                Fills in a ready-made personality below — edit anything you like afterwards.
              </p>
              <div className="grid grid-cols-2 gap-2">
                {templates.map(t => (
                  <button
                    type="button"
                    key={t.id}
                    onClick={() => applyTemplate(t)}
                    className={`text-left rounded-xl border px-3.5 py-3 transition-all ${
                      selectedTemplateId === t.id
                        ? 'border-violet-500/60 bg-violet-500/10'
                        : 'border-white/10 bg-white/5 hover:bg-white/8'
                    }`}
                  >
                    <div className="text-sm font-semibold text-white">{t.name}</div>
                    <div className="text-xs text-slate-500 mt-0.5">{t.description}</div>
                  </button>
                ))}
              </div>
              {selectedTemplateId && (
                <button type="button" onClick={() => setSelectedTemplateId(null)}
                  className="text-xs text-slate-500 hover:text-slate-300 mt-3">
                  Clear selection — start blank instead
                </button>
              )}
            </div>
          )}

          {/* Bot Name */}
          <div className="bg-white/4 border border-white/8 rounded-2xl p-5">
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Bot Name</label>
            <input
              required
              value={form.name}
              onChange={e => set('name', e.target.value)}
              placeholder="e.g. Nitya"
              className="w-full bg-white/5 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white placeholder-slate-600 outline-none focus:border-violet-500/60 focus:ring-1 focus:ring-violet-500/30 transition-all"
            />
          </div>

          {/* System Prompt */}
          <div className="bg-white/4 border border-white/8 rounded-2xl p-5">
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Personality / System Prompt</label>
            <textarea
              required
              rows={4}
              value={form.system_prompt}
              onChange={e => set('system_prompt', e.target.value)}
              placeholder="Describe the bot's personality and role…"
              className="w-full bg-white/5 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white placeholder-slate-600 outline-none focus:border-violet-500/60 focus:ring-1 focus:ring-violet-500/30 transition-all resize-none"
            />
          </div>

          {/* Voice */}
          <div className="bg-white/4 border border-white/8 rounded-2xl p-5">
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Voice</label>
            <div className="space-y-2">
              {voicesFor(form.language).map(v => (
                <label
                  key={v.id}
                  className={`flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all ${
                    form.voice_id === v.id
                      ? 'border-violet-500/50 bg-violet-500/10'
                      : 'border-white/8 hover:border-white/15 hover:bg-white/4'
                  }`}
                >
                  <input
                    type="radio"
                    name="voice"
                    value={v.id}
                    checked={form.voice_id === v.id}
                    onChange={() => set('voice_id', v.id)}
                    className="accent-violet-500"
                  />
                  <div>
                    <p className="text-sm font-medium text-white">{v.label}</p>
                    <p className="text-xs text-slate-500">{v.desc}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Tools — Task 3.1. A separate page rather than a section here:
              a tool has a dozen fields of its own, and burying that inside an
              already-long settings form would make both harder to use. New
              bots have no id to hang tools off yet, so it appears after the
              first save. */}
          {!isNew && (
            <button
              type="button"
              onClick={() => navigate(`/bots/${id}/tools`)}
              className="w-full bg-white/4 border border-white/8 rounded-2xl p-5 flex items-center gap-4 text-left hover:border-violet-500/40 hover:bg-white/6 transition-all"
            >
              <div className="flex-1">
                <p className="font-semibold text-white text-sm">Tools</p>
                <p className="text-xs text-slate-500 mt-0.5">
                  Let this bot look things up, book slots, or call your own systems
                </p>
              </div>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="text-slate-500">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>
          )}

          {/* Language + Model */}
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-white/4 border border-white/8 rounded-2xl p-5">
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Language</label>
              <select
                value={form.language}
                onChange={e => setLanguage(e.target.value)}
                className="w-full bg-white/5 border border-white/10 rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-violet-500/60 transition-all"
              >
                {LANGUAGES.map(l => (
                  <option key={l.code} value={l.code} className="bg-neutral-900">
                    {l.flag} {l.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="bg-white/4 border border-white/8 rounded-2xl p-5">
              <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">AI Model</label>
              <select
                value={form.llm_model}
                onChange={e => set('llm_model', e.target.value)}
                className="w-full bg-white/5 border border-white/10 rounded-xl px-3 py-2.5 text-sm text-white outline-none focus:border-violet-500/60 transition-all"
              >
                {MODELS.map(m => (
                  <option key={m.id} value={m.id} className="bg-neutral-900">
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Knowledge Base — only shown when editing an existing bot */}
          {!isNew && (
            <div className="bg-white/4 border border-white/8 rounded-2xl p-5">
              <div className="flex items-center justify-between mb-3">
                <div>
                  <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider">Knowledge Base</label>
                  <p className="text-xs text-slate-600 mt-0.5">Upload PDFs — the bot will answer questions from them</p>
                </div>
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading}
                  className="flex items-center gap-1.5 bg-violet-600/70 hover:bg-violet-500 disabled:opacity-50 text-white text-xs font-semibold px-3 py-1.5 rounded-xl transition-all"
                >
                  {uploading ? (
                    <span className="w-3 h-3 border border-white/40 border-t-white rounded-full animate-spin" />
                  ) : (
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                  )}
                  {uploading ? 'Uploading…' : 'Upload PDF'}
                </button>
                <input ref={fileInputRef} type="file" accept=".pdf" className="hidden" onChange={handleUpload} />
              </div>

              {uploadError && (
                <p className="text-xs text-red-400 mb-2">{uploadError}</p>
              )}

              {docs.length === 0 ? (
                <div className="border border-dashed border-white/8 rounded-xl p-5 text-center">
                  <p className="text-xs text-slate-600">No documents uploaded yet</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {docs.map(doc => (
                    <div key={doc.id} className="flex items-center justify-between bg-white/4 border border-white/8 rounded-xl px-3 py-2.5">
                      <div className="flex items-center gap-2.5 min-w-0">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
                        </svg>
                        <div className="min-w-0">
                          <p className="text-xs font-medium text-white truncate">{doc.filename}</p>
                          <p className="text-xs text-slate-600">{doc.chunk_count} chunks indexed</p>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleDeleteDoc(doc.id)}
                        className="p-1 text-slate-600 hover:text-red-400 transition-colors ml-2 shrink-0"
                      >
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
                        </svg>
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Actions */}
          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={() => navigate('/dashboard')}
              className="flex-1 border border-white/10 hover:border-white/20 text-slate-400 hover:text-white font-semibold rounded-xl py-2.5 text-sm transition-all"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex-1 bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 disabled:opacity-50 text-white font-semibold rounded-xl py-2.5 text-sm transition-all shadow-lg shadow-violet-900/30"
            >
              {saving ? 'Saving…' : isNew ? 'Create Bot' : 'Save Changes'}
            </button>
          </div>
        </form>
      </main>
    </div>
  )
}
