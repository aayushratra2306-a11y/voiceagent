import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { Outlet, useParams, Navigate } from 'react-router-dom'
import { listOrgs, setActiveOrg, type Org } from '../lib/api'
import { atLeast, writeLastOrg, type Role } from '../lib/orgs'
import PageLoader from '../components/PageLoader'

interface OrgValue {
  orgId: string
  org: Org
  orgs: Org[]
  role: Role
  can: (minimum: Role) => boolean
  orgPath: (to: string) => string
  reloadOrgs: () => Promise<void>
}

const Ctx = createContext<OrgValue | null>(null)

export function useOrg(): OrgValue {
  const value = useContext(Ctx)
  if (!value) throw new Error('useOrg must be used inside an OrgProvider')
  return value
}

/**
 * The organisation lives in the address, so it is per-tab and shareable.
 *
 * This is a layout route: it renders under /o/:orgId and every signed-in
 * page renders inside it. Three things follow from that. The API client is
 * told the organisation while THIS component renders, before any child
 * effect can fire a request — an effect here would be one render too late
 * and the first request of a page would go out unscoped. The membership
 * list is fetched once and shared, so the switcher and the role checks do
 * not each poll. And an organisation that is not mine is decided here,
 * client-side from GET /orgs, rather than by waiting for a 404 from
 * whatever the page happened to request first.
 */
export function OrgProvider() {
  const { orgId } = useParams<{ orgId: string }>()
  const [orgs, setOrgs] = useState<Org[] | null>(null)
  const [failed, setFailed] = useState(false)

  // Deliberately during render, not in an effect. Assigning a module
  // variable is idempotent and has no React state attached to it, so a
  // double render in StrictMode simply writes the same value twice.
  setActiveOrg(orgId ?? null)

  const load = useCallback(async () => {
    try {
      setOrgs(await listOrgs())
      setFailed(false)
    } catch {
      setFailed(true)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    // Clear it on the way out so a page rendered outside any organisation
    // (the chooser, the login screen) cannot inherit a stale one.
    return () => setActiveOrg(null)
  }, [])

  const orgPath = useCallback(
    (to: string) => `/o/${orgId}${to.startsWith('/') ? to : `/${to}`}`,
    [orgId],
  )

  if (failed) {
    return (
      <div className="min-h-screen bg-[#070711] text-slate-300 flex items-center justify-center p-6">
        <div className="text-center">
          <p className="mb-3">Could not load your organisations.</p>
          <button onClick={() => void load()} className="px-4 py-2 rounded-lg bg-white/10 hover:bg-white/15">
            Try again
          </button>
        </div>
      </div>
    )
  }

  if (orgs === null) return <PageLoader />

  const org = orgs.find(o => o.id === orgId)
  // Not a member (or the id is nonsense): the chooser explains it. Sending
  // them to /login instead — which is what the old catch-all did — logs a
  // perfectly valid session out over a mistyped address.
  if (!org) return <Navigate to="/o" replace />

  writeLastOrg(org.id)

  const value: OrgValue = {
    orgId: org.id,
    org,
    orgs,
    role: org.role,
    can: (minimum: Role) => atLeast(org.role, minimum),
    orgPath,
    reloadOrgs: load,
  }

  return (
    <Ctx.Provider value={value}>
      <Outlet />
    </Ctx.Provider>
  )
}

export default OrgProvider
