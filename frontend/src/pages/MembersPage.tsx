import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  listMembers, setMemberRole, removeMember,
  inviteMember, listInvitations, revokeInvitation,
  type Member, type Invitation,
} from '../lib/api'
import { useOrg } from '../context/OrgContext'
import { atLeast, type Role } from '../lib/orgs'
import { usePageChrome } from '../context/ChromeContext'
import { useAuth } from '../context/AuthContext'
import PageLoader from '../components/PageLoader'

const ROLES: Role[] = ['viewer', 'member', 'admin', 'owner']

function formatDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, {
    year: 'numeric', month: 'long', day: 'numeric',
  })
}

export default function MembersPage() {
  const { orgId, org, role, can, orgPath, reloadOrgs } = useOrg()
  const { email: myEmail } = useAuth()
  const navigate = useNavigate()

  const [members, setMembers] = useState<Member[] | null>(null)
  const [error, setError] = useState('')
  const [addEmail, setAddEmail] = useState('')
  const [newRole, setNewRole] = useState<Role>('member')
  const [busy, setBusy] = useState(false)

  // Task 5.2 — the result of the most recent successful invite, shown as a
  // copyable absolute link. Cleared on a fresh submit so a second invite
  // doesn't leave the previous one's link on screen looking current.
  const [inviteLink, setInviteLink] = useState<{ url: string; expiresAt: string } | null>(null)
  const [invitations, setInvitations] = useState<Invitation[]>([])

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

  // Only admins/owners can act on invitations, and the list endpoint is
  // gated the same way server-side — skip the request entirely for anyone
  // else rather than firing a call that will just 403.
  const loadInvitations = useCallback(async () => {
    if (!manages) { setInvitations([]); return }
    try {
      setInvitations(await listInvitations(orgId))
    } catch (e) {
      setError((e as Error).message)
    }
  }, [orgId, manages])

  useEffect(() => { void loadInvitations() }, [loadInvitations])
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

  // Kept separate from `run()`: an invite doesn't change the member list or
  // this user's own org roles, so there's no reason to re-fetch either —
  // it only needs the pending-invitations list refreshed afterward.
  async function submitInvite(e: React.FormEvent) {
    e.preventDefault()
    const email = addEmail.trim()
    if (!email) return
    setInviteLink(null)
    setBusy(true)
    setError('')
    try {
      const result = await inviteMember(orgId, email, newRole)
      setAddEmail('')
      // invite_path is a bare path ("/invite/<token>"); the link a person
      // can actually be sent needs this page's own origin in front of it.
      setInviteLink({ url: window.location.origin + result.invite_path, expiresAt: result.expires_at })
      await loadInvitations()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function revoke(id: string) {
    setBusy(true)
    setError('')
    try {
      await revokeInvitation(orgId, id)
      await loadInvitations()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function copyInviteLink(url: string) {
    try {
      await navigator.clipboard.writeText(url)
    } catch {
      // Clipboard access can be blocked (permissions, insecure context,
      // jsdom in tests) — the link is still visible and selectable above,
      // so this is a convenience, not the only way to get it.
    }
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
        <div className="mb-8">
          <form onSubmit={e => void submitInvite(e)} className="flex flex-wrap gap-2">
            <input
              aria-label="Email address"
              value={addEmail}
              onChange={e => setAddEmail(e.target.value)}
              placeholder="colleague@company.com"
              className="flex-1 min-w-[200px] px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm"
            />
            <select
              aria-label="Role for the invitation"
              value={newRole}
              onChange={e => setNewRole(e.target.value as Role)}
              className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm"
            >
              {assignableRoles.map(r => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
            <button type="submit" disabled={busy} className="px-4 py-2 rounded-lg bg-cyan-500/20 text-cyan-200 text-sm disabled:opacity-50">
              Create invitation
            </button>
          </form>

          {inviteLink && (
            <div className="mt-3 px-4 py-3 rounded-xl bg-cyan-500/10 border border-cyan-500/20 text-sm space-y-2">
              <p className="text-cyan-100">
                Invitation created. Nobody has been notified — copy this link and send it to them yourself.
              </p>
              <div className="flex flex-wrap gap-2 items-center">
                <input
                  aria-label="Invitation link"
                  readOnly
                  value={inviteLink.url}
                  onFocus={e => e.target.select()}
                  className="flex-1 min-w-[200px] px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-xs font-mono"
                />
                <button
                  type="button"
                  onClick={() => void copyInviteLink(inviteLink.url)}
                  className="px-3 py-2 rounded-lg bg-white/10 text-xs hover:bg-white/20"
                >
                  Copy link
                </button>
              </div>
              <p className="text-xs text-slate-400">Expires {formatDate(inviteLink.expiresAt)}.</p>
            </div>
          )}
        </div>
      )}

      {manages && invitations.length > 0 && (
        <div className="mb-8">
          <h2 className="text-sm font-medium text-slate-300 mb-2">Pending invitations</h2>
          <ul className="space-y-2">
            {invitations.map(inv => (
              <li key={inv.id} className="flex items-center gap-3 px-4 py-3 rounded-xl bg-white/5 border border-white/10">
                <span className="flex-1 text-sm truncate">{inv.email}</span>
                <span className="text-xs text-slate-500">{inv.role}</span>
                <span className="text-xs text-slate-600">Expires {formatDate(inv.expires_at)}</span>
                <button
                  aria-label={`Revoke invitation to ${inv.email}`}
                  disabled={busy}
                  onClick={() => void revoke(inv.id)}
                  className="text-xs text-slate-500 hover:text-rose-300 disabled:opacity-50"
                >
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        </div>
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
