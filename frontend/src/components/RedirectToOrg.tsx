import { useEffect, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { listOrgs, type Org } from '../lib/api'
import { readLastOrg } from '../lib/orgs'
import PageLoader from './PageLoader'

/**
 * Keeps every address that existed before 5.1 working.
 *
 * /dashboard, /bots/:id, /bots/new, /bots/:id/tools, /session/:id,
 * /webhooks and /approvals all still resolve — they go to the same page
 * under the last organisation this browser used, or the personal one, or
 * the first one. A bookmark from last week still opens the right screen.
 */
export default function RedirectToOrg() {
  const location = useLocation()
  const [orgs, setOrgs] = useState<Org[] | null>(null)

  useEffect(() => {
    let cancelled = false
    listOrgs()
      .then(list => { if (!cancelled) setOrgs(list) })
      .catch(() => { if (!cancelled) setOrgs([]) })
    return () => { cancelled = true }
  }, [])

  if (orgs === null) return <PageLoader />
  if (orgs.length === 0) return <Navigate to="/o" replace />

  const remembered = readLastOrg()
  const target =
    orgs.find(o => o.id === remembered) ??
    orgs.find(o => o.personal) ??
    orgs[0]

  return <Navigate to={`/o/${target.id}${location.pathname}${location.search}`} replace />
}
