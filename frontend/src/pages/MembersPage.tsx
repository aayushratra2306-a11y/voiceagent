import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listMembers, addMember, setMemberRole, removeMember, type Member } from '../lib/api'
import { useOrg } from '../context/OrgContext'
import { atLeast, type Role } from '../lib/orgs'
import { usePageChrome } from '../context/ChromeContext'
import { useAuth } from '../context/AuthContext'
import PageLoader from '../components/PageLoader'

const ROLES: Role[] = ['viewer', 'member', 'admin', 'owner']

export default function MembersPage() {
  const { orgId, org, role, can, orgPath, reloadOrgs } = useOrg()
  const { email: myEmail } = useAuth()
  const navigate = useNavigate()

  const [members, setMembers] = useState<Member[] | null>(null)
  const [error, setError] = useState('')
  const [addEmail, setAddEmail] = useState('')
  const [newRole, setNewRole] = useState<Role>('member')
  const [busy, setBusy] = useState(false)

  usePageChrome('Members', orgPath('/dashboard'))

  const load = useCallback(async () => {
    try {
      setMembers(await listMembers(orgId))
    } catch (e) {
      setError((e as Error).message)
      setMembers([])
    }
  }, [orgId])

  useEffect(() => { void load() }, [load])

  const manages = can('admin')
  // An admin may not touch an owner — the server refuses it, and offering
  // the control anyway would just be a button that always fails.
  const mayTouch = (m: Member) => manages && (role === 'owner' || m.role !== 'owner')
  // Same rule for what an admin may hand out: they can't promote anyone to
  // owner either.
  const assignableRoles = role === 'owner' ? ROLES : ROLES.filter(r => r !== 'owner')

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError('')
    try {
      await action()
      await load()
      await reloadOrgs()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function submitAdd(e: React.FormEvent) {
    e.preventDefault()
    if (!addEmail.trim()) return
    await run(async () => {
      await addMember(orgId, addEmail.trim(), newRole)
      setAddEmail('')
    })
  }

  async function leave() {
    const me = members?.find(m => m.email === myEmail)
    if (!me) { setError('Could not work out which member you are.'); return }
    await run(async () => {
      // The server treats "remove yourself" as leaving, whatever your role
      // — except for the last owner, which it refuses. Its message is shown.
      await removeMember(orgId, me.user_id)
      navigate('/o', { replace: true })
    })
  }

  if (members === null) return <PageLoader />

  return (
    <div className="relative z-10 max-w-3xl mx-auto px-6 py-8">
      <h1 className="text-lg font-semibold mb-1">Members</h1>
      <p className="text-sm text-slate-400 mb-6">Who can use {org.name}, and what they can do.</p>

      {error && <p className="mb-4 text-sm text-rose-300">{error}</p>}

      <ul className="space-y-2 mb-8">
        {members.map(m => (
          <li key={m.user_id} className="flex items-center gap-3 px-4 py-3 rounded-xl bg-white/5 border border-white/10">
            <span className="flex-1 text-sm truncate">{m.email}</span>
            {mayTouch(m) ? (
              <select
                aria-label={`Role for ${m.email}`}
                value={m.role}
                disabled={busy}
                onChange={e => void run(() => setMemberRole(orgId, m.user_id, e.target.value as Role))}
                className="text-xs bg-white/5 border border-white/10 rounded-lg px-2 py-1"
              >
                {assignableRoles.map(r => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
            ) : (
              <span className="text-xs text-slate-500">{m.role}</span>
            )}
            {mayTouch(m) && (
              <button
                aria-label={`Remove ${m.email}`}
                disabled={busy}
                onClick={() => void run(() => removeMember(orgId, m.user_id))}
                className="text-xs text-slate-500 hover:text-rose-300 disabled:opacity-50"
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>

      {manages && (
        <form onSubmit={e => void submitAdd(e)} className="flex flex-wrap gap-2 mb-8">
          <input
            aria-label="Email address"
            value={addEmail}
            onChange={e => setAddEmail(e.target.value)}
            placeholder="colleague@company.com"
            className="flex-1 min-w-[200px] px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm"
          />
          <select
            aria-label="Role for the new member"
            value={newRole}
            onChange={e => setNewRole(e.target.value as Role)}
            className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm"
          >
            {assignableRoles.map(r => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
          <button type="submit" disabled={busy} className="px-4 py-2 rounded-lg bg-cyan-500/20 text-cyan-200 text-sm disabled:opacity-50">
            Add member
          </button>
        </form>
      )}

      <button
        onClick={() => void leave()}
        disabled={busy}
        className="text-xs text-slate-500 hover:text-rose-300 disabled:opacity-50"
      >
        Leave organisation
      </button>
      {atLeast(role, 'owner') && (
        <p className="mt-2 text-xs text-slate-600">
          An organisation always keeps at least one owner, so the last one cannot leave.
        </p>
      )}
    </div>
  )
}
