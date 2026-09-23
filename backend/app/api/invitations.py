"""Task 5.2 (Task 3) — the invitee side: the two routes someone reaches by
holding a raw invitation token, not by any session- or org-scoped
credential. Task 1 (app/services/invitations.py) and Task 2
(app/api/orgs.py's admin-facing invite/list/revoke) are done and are not
touched here.

Neither route below is org-scoped (see tests/test_org_route_coverage.py's
EXEMPT list): the token itself names the organisation, so there is nothing
else to scope by, and get_org_context/get_org_context_from_path would have
nowhere to get an org id from anyway (it's not in the path or a header —
it's inside the token).
"""

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.models.invitation import Invitation
from app.models.organisation import Organisation
from app.models.user import User
from app.services import invitations as inv

router = APIRouter(prefix="/invitations", tags=["invitations"])

# Reused across every "this token is unusable" response, in and out of both
# routes below — see find_valid's docstring: unknown, expired, revoked and
# already-accepted are ALL this one exception, on purpose, so a caller
# (anyone, for the GET route — it needs no login) learns nothing about
# which of those four is true. Sharing one exception object, the way
# app/core/auth.py's credentials_exception does, is a small extra guard
# against two call sites ever drifting into two different bodies.
_NOT_FOUND = HTTPException(status_code=404, detail="Invitation not found")


@router.get("/{token}")
async def preview_invitation(token: str):
    """No authentication — this is the link someone clicks before they have
    ever logged in.

    What comes back, and why that's the line: enough for the invitee to
    decide the link is real —

      - org_name: what they're being asked to join
      - role: what they'd be able to do there
      - email: which address this invitation was sent to (so they can tell
        "yes, that's me" from "this isn't mine")
      - invited_by_email: who's asking — a bare "you were invited to
        Acme" is exactly the shape a phishing page would also produce;
        naming the specific person makes the link verifiable against a
        conversation the invitee actually had

    and nothing else: no member list, no org size, no other pending
    invitations, no ids. All four fields are already implied by possessing
    this token — a random 256-bit value that is single-use, sent to one
    address, hashed at rest, and expires in 7 days — so none of them are
    new information handed to a stranger who merely guesses at tokens
    (infeasible at that entropy) or who intercepts a link that was in fact
    meant for them. The one field worth a second thought is
    invited_by_email: it does name a real person's address to whoever holds
    the link, which matters if a link is forwarded somewhere it shouldn't
    be. It stays in because that's the entire point of the route per the
    task brief (letting the invitee verify who's asking), and because the
    exposure is gated behind the same unguessable token as everything
    else here, not behind anything a stranger can reach on their own.
    """
    try:
        invite = await inv.find_valid(token)
    except inv.NotFoundError as e:
        raise _NOT_FOUND from e

    org = await Organisation.get(invite.org_id)
    inviter = await User.get(invite.invited_by)
    if org is None or inviter is None:
        # Defensive, not currently reachable via any exercised path: delete_org
        # does not check for pending invitations before deleting an
        # organisation, so a stale invitation COULD outlive its org or (if
        # accounts were ever deletable) its inviter. Whatever the reason the
        # referenced row is gone, the token still cannot actually be used, so
        # it gets the same answer as any other unusable token rather than a
        # 500 or a differently-shaped 404.
        raise _NOT_FOUND
    return {
        "org_name": org.name,
        "role": invite.role,
        "email": invite.email,
        "invited_by_email": inviter.email,
    }


@router.post("/{token}/accept")
async def accept_invitation(token: str, user: User = Depends(get_current_user)):
    """Authentication required — this is what actually joins the org.
    accept() (Task 1) does the real work: matches the invited email against
    the signed-in user, adds the membership, then marks the invitation
    accepted, in that order, with no transaction (Atlas M0 has none usable).

    Two different double-submit shapes exist here, and they get different
    answers on purpose:

    - The race accept() itself documents: interrupted between add_member
      succeeding and the invitation being saved as accepted. A retry finds
      the invitation still "pending", matches the email again, and
      add_member raises AlreadyMemberError — surfaced here as 409.

    - An ordinary clean double-submit (a double-click, a retried request)
      after a first accept has already fully completed: the invitation is
      already "accepted", so find_valid inside accept() no longer finds it
      "pending" and raises the same NotFoundError as any unusable token.
      Rather than hand a plain 404 to someone who just legitimately joined,
      the fallback below re-checks for exactly that one case — this token,
      already accepted, by this same authenticated user — before falling
      back to the uniform 404. Anyone else presenting this same used-up
      token (a stranger, or a different signed-in account) does not match
      that accepted_by check and still gets the plain 404, so nothing here
      weakens the indistinguishability the GET route relies on: only the
      person who actually redeemed the token gets the friendlier answer,
      and it costs them nothing they don't already know.
    """
    try:
        invite = await inv.accept(token, user)
    except inv.EmailMismatchError as e:
        # The link is a bearer credential; matching the signed-in user's
        # email against the invited address limits the damage of a
        # forwarded link. Clear and specific is fine here (unlike the GET
        # route) because reaching this branch already required a valid,
        # not-yet-used token AND a login — there is no anonymous probing
        # surface left to protect by staying vague.
        raise HTTPException(
            status_code=403, detail="This invitation was sent to a different email address"
        ) from e
    except inv.AlreadyMemberError as e:
        raise HTTPException(status_code=409, detail="You're already a member of this organisation") from e
    except inv.NotFoundError as e:
        stale = await Invitation.find_one(
            Invitation.token_hash == inv.hash_token(token),
            Invitation.status == "accepted",
            Invitation.accepted_by == str(user.id),
        )
        if stale is None:
            raise _NOT_FOUND from e
        return {"org_id": stale.org_id, "role": stale.role}

    return {"org_id": invite.org_id, "role": invite.role}
