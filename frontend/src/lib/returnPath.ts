/**
 * Task 5.2 — `?next=` carries an invitee back to their invitation after
 * they sign in or sign up.
 *
 * It lives here, as its own function, rather than inline in LoginPage,
 * because "where do we send someone after login" is precisely the input an
 * attacker would love to control: a login page that forwards to an
 * arbitrary URL is an open redirect, and a convincing one, because the
 * victim really did just sign in to the real site.
 *
 * Today LoginPage hands the result to React Router's navigate(), which
 * cannot leave the origin whatever it is given — so a bad value is not
 * exploitable right now. That is a property of the CALL SITE, not of the
 * value, and `window.location.href = next` is one refactor away. The guard
 * and its tests exist so that refactor stays safe.
 */
export function safeReturnPath(next: string | null | undefined): string | null {
  if (!next) return null
  // Must be a path on this site: one leading slash, and not two — "//host"
  // is protocol-relative and goes straight off-origin. That also rejects
  // "https://evil.com", "javascript:..." and a bare "evil.com", none of
  // which start with a slash at all.
  if (!next.startsWith('/') || next.startsWith('//')) return null
  // A backslash after the first slash is treated as a separator by some
  // browsers, so "/\evil.com" can behave like "//evil.com".
  if (next.startsWith('/\\')) return null
  return next
}
