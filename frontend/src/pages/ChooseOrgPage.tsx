import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listOrgs, createOrg, type Org } from '../lib/api'
import { writeLastOrg } from '../lib/orgs'
import PageLoader from '../components/PageLoader'

/**
 * Shown when the address names no organisation, or one that is not mine.
 *
 * Zero organisations is its own state rather than a blank page: it can only
 * happen if the backend's self-heal failed, and "setting up your workspace"
 * with a retry is the honest description of that.
 */
export default function ChooseOrgPage() {
  const [orgs, setOrgs] = useState<Org[] | null>(null)
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  async function load() {
    setError('')
    try {
      setOrgs(await listOrgs())
    } catch (e) {
      setError((e as Error).message)
      setOrgs([])
    }
  }

  useEffect(() => { void load() }, [])

  function go(org: Org) {
    writeLastOrg(org.id)
    navigate(`/o/${org.id}/dashboard`)
  }

  async function create(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      go(await createOrg(name.trim()))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (orgs === null) return <PageLoader />

  return (
    <div className="min-h-screen bg-[#070711] text-white flex items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <h1 className="text-lg font-semibold mb-1">Choose an organisation</h1>
        {orgs.length === 0 ? (
          <div className="text-sm text-slate-400 mb-4">
            Setting up your workspace.
            <button onClick={() => void load()} className="ml-2 underline hover:text-slate-200">Retry</button>
          </div>
        ) : (
          <p className="text-sm text-slate-400 mb-4">Pick the workspace you want to open.</p>
        )}

        <ul className="space-y-2 mb-6">
          {orgs.map(o => (
            <li key={o.id}>
              <button
                onClick={() => go(o)}
                className="w-full text-left px-4 py-3 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10"
              >
                <span className="block text-sm">{o.name}</span>
                <span className="block text-xs text-slate-500">{o.role}</span>
              </button>
            </li>
          ))}
        </ul>

        <form onSubmit={create} className="flex gap-2">
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="New organisation name"
            aria-label="New organisation name"
            className="flex-1 px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm"
          />
          <button type="submit" disabled={busy} className="px-4 py-2 rounded-lg bg-cyan-500/20 text-cyan-200 text-sm disabled:opacity-50">
            Create
          </button>
        </form>

        {error && <p className="mt-3 text-sm text-rose-300">{error}</p>}
      </div>
    </div>
  )
}
