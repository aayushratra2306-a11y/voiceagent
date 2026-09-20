// Roles, in the order the backend ranks them (app/models/organisation.py).
// The UI uses this only to hide controls a role cannot use — the server
// enforces the same table and remains the authority.
export type Role = 'owner' | 'admin' | 'member' | 'viewer'

export const ROLE_RANK: Record<Role, number> = {
  viewer: 0,
  member: 1,
  admin: 2,
  owner: 3,
}

export function atLeast(role: Role | null | undefined, minimum: Role): boolean {
  if (!role || !(role in ROLE_RANK)) return false
  return ROLE_RANK[role] >= ROLE_RANK[minimum]
}

// The last organisation this browser used, so an old address (/dashboard)
// can be redirected somewhere sensible. A per-viewer convenience only: it
// is never the source of truth for a request, and every read is wrapped
// because storage can be unavailable or blocked.
const LAST_ORG_KEY = 'voix:last-org'

export function readLastOrg(): string | null {
  try {
    return localStorage.getItem(LAST_ORG_KEY)
  } catch {
    return null
  }
}

export function writeLastOrg(id: string): void {
  try {
    localStorage.setItem(LAST_ORG_KEY, id)
  } catch {
    // A private window with storage blocked still works; it just forgets.
  }
}
