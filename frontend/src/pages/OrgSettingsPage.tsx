import { useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { renameOrg, deleteOrg } from '../lib/api'
import { useOrg } from '../context/OrgContext'
import { usePageChrome } from '../context/ChromeContext'

export default function OrgSettingsPage() {
  const { orgId, org, orgs, role, can, orgPath, reloadOrgs } = useOrg()
  const navigate = useNavigate()

  const [name, setName] = useState(org.name)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)

  usePageChrome('Settings', orgPath('/dashboard'))

  const isLastOrg = orgs.length <= 1

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true); setError(''); setSaved(false)
    try {
      await renameOrg(orgId, name.trim())
      await reloadOrgs()
      setSaved(true)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    setBusy(true); setError('')
    try {
      await deleteOrg(orgId)
      await reloadOrgs()
      navigate('/o', { replace: true })
    } catch (e) {
      setError((e as Error).message)
      setConfirming(false)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative z-10 max-w-xl mx-auto px-6 py-8">
      <h1 className="text-lg font-semibold mb-6">Organisation settings</h1>

      <form onSubmit={e => void save(e)} className="mb-10">
        <label htmlFor="org-name" className="block text-xs text-slate-400 mb-1.5">Organisation name</label>
        <div className="flex gap-2">
          <input
            id="org-name"
            value={name}
            onChange={e => { setName(e.target.value); setSaved(false) }}
            disabled={!can('admin') || busy}
            className="flex-1 px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm disabled:opacity-60"
          />
          {can('admin') && (
            <button type="submit" disabled={busy} className="px-4 py-2 rounded-lg bg-cyan-500/20 text-cyan-200 text-sm disabled:opacity-50">
              Save name
            </button>
          )}
        </div>
        {!can('admin') && (
          <p className="mt-1.5 text-xs text-slate-600">Your role ({role}) can&rsquo;t rename this organisation.</p>
        )}
        {saved && <p className="mt-1.5 text-xs text-emerald-300">Saved.</p>}
      </form>

      <div className="border-t border-white/8 pt-6">
        <h2 className="text-sm font-semibold mb-1.5">Delete this organisation</h2>

        {role !== 'owner' ? (
          <p className="text-xs text-slate-500">Only an owner can delete an organisation.</p>
        ) : isLastOrg ? (
          <p className="text-xs text-slate-500">
            This is your only organisation, so it can&rsquo;t be deleted — you would be left with nowhere to work.
          </p>
        ) : confirming ? (
          <div className="flex items-center gap-2">
            <button onClick={() => void remove()} disabled={busy} className="px-3 py-1.5 rounded-lg bg-rose-500/20 text-rose-200 text-xs disabled:opacity-50">
              Yes, delete it
            </button>
            <button onClick={() => setConfirming(false)} disabled={busy} className="px-3 py-1.5 rounded-lg bg-white/5 text-slate-300 text-xs">
              Cancel
            </button>
          </div>
        ) : (
          <>
            <p className="text-xs text-slate-500 mb-2">
              Only an empty organisation can be deleted: no bots, no webhooks and no waiting approvals.
            </p>
            <button onClick={() => setConfirming(true)} className="px-3 py-1.5 rounded-lg bg-white/5 text-slate-300 hover:text-rose-300 text-xs">
              Delete organisation
            </button>
          </>
        )}

        {error && <p className="mt-3 text-sm text-rose-300">{error}</p>}
      </div>
    </div>
  )
}
