/**
 * Window events the app uses to talk between two components that have no
 * parent/child relationship — the Approvals page and the header badge sit
 * in different halves of the tree.
 *
 * Deliberately not a context: the count already has an owner (the shell)
 * and a source of truth (the API). This only says "ask again now".
 */
export const APPROVALS_CHANGED = 'auris:approvals-changed'
