"""Task 4.8 — create and remove the disposable accounts a load test calls through.

The load test needs one account per simultaneous call: the server gives each
account exactly one live call and ends the previous one when the same account
starts another (connect.py, _end_previous_calls_for). Each account gets one
bot, with no knowledge base — in rehearsal mode the knowledge base is off
anyway (app/services/rag.py), and a bot with documents would only add
Pinecone cost to a test that exists to avoid it.

Two halves, run in two different places, deliberately:

  create   from anywhere, against the public API over HTTPS. It signs up like
           any customer would, so it exercises the real registration path and
           needs no database access at all.

  delete   on the SERVER, inside the backend container, because there is no
           delete-account endpoint and the database login that can remove a
           user only exists there. It is a production write: it names what it
           would delete and does nothing until told --yes.

Addresses live in loadtest.example.com. example.com is reserved by RFC 2606
and can never be registered by anyone, so an address in it cannot collide
with a real customer — which is what makes the deletion filter safe.

    python -m scripts.loadtest_accounts create --base-url https://your-domain \
        --count 8 --out loadtest_accounts.json
    python -m scripts.loadtest_accounts delete            # lists, deletes nothing
    python -m scripts.loadtest_accounts delete --yes      # actually removes them

The password is read from LOADTEST_PASSWORD or a hidden prompt, never from
the command line, which would put it in shell history and the process list.
"""

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from typing import NamedTuple

# Reserved by RFC 2606: nobody can register it, so no real customer can ever
# hold an address here. This is the whole basis for the deletion filter being
# safe — see tests/test_loadtest_accounts.py.
LOADTEST_DOMAIN = "loadtest.example.com"

# The register and login routes both allow 5 a minute from one address
# (auth.py). Creating accounts any faster just collects 429s.
SIGNUP_SPACING_SECONDS = 13.0

MIN_PASSWORD_LENGTH = 12  # auth.py's RegisterRequest


def account_email(run_id: str, index: int) -> str:
    return f"lt-{run_id}-{index}@{LOADTEST_DOMAIN}"


def is_loadtest_account(email: str) -> bool:
    """Suffix match, not a substring search: `user@notloadtest.example.com`
    and `loadtest.example.com@gmail.com` both contain the domain and belong
    to somebody real."""
    return email.lower().endswith("@" + LOADTEST_DOMAIN)


class Targets(NamedTuple):
    user_ids: list[str]
    bot_ids: list[str]
    membership_ids: list[str]
    org_ids: list[str]


def loadtest_targets(users, bots, memberships, organisations) -> Targets:
    """Decide what the cleanup will remove, given everything that exists.

    A plain function over already-fetched rows, deliberately: this is the
    half that runs against production, so which rows it picks can be checked
    without a database in the way (tests/test_loadtest_accounts.py).

    The bot ids are returned because the transcripts each test call wrote are
    reachable only by bot_id. Collect them here, before anything is deleted,
    or those rows are stranded with nothing left linking them to a test
    account.

    Task 5.1 — every load-test bot now lives in an organisation
    (loadtest_accounts.create), so a clean delete removes the load-test
    user's memberships too, and any organisation THEY created that would be
    left with no members once those memberships are gone. Never an
    organisation merely touched by a load-test membership: one they were
    invited into (or a real organisation whose other members remain) is
    left alone.
    """
    user_ids = [str(u.id) for u in users if is_loadtest_account(u.email)]
    wanted = set(user_ids)
    bot_ids = [str(b.id) for b in bots if str(b.user_id) in wanted]
    membership_ids = [str(m.id) for m in memberships if m.user_id in wanted]

    remaining_members_by_org: dict[str, int] = {}
    for m in memberships:
        if m.user_id not in wanted:
            remaining_members_by_org[m.org_id] = remaining_members_by_org.get(m.org_id, 0) + 1
    org_ids = [
        str(o.id) for o in organisations
        if o.created_by in wanted and remaining_members_by_org.get(str(o.id), 0) == 0
    ]
    return Targets(user_ids=user_ids, bot_ids=bot_ids, membership_ids=membership_ids, org_ids=org_ids)


def _password() -> str:
    password = os.environ.get("LOADTEST_PASSWORD") or getpass.getpass(
        "Password for the load-test accounts (hidden): "
    )
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"That password is shorter than the {MIN_PASSWORD_LENGTH} characters the server requires.")
    return password


# ---------------------------------------------------------------------------
# create — over the public API, from anywhere
# ---------------------------------------------------------------------------


async def create(base_url: str, count: int, out: Path) -> None:
    import aiohttp

    password = _password()
    run_id = secrets.token_hex(3)
    accounts = []

    async with aiohttp.ClientSession() as session:
        for i in range(count):
            if i:
                # Paced rather than retried: the rate limit is per address and
                # we know the rate, so waiting is simpler than handling 429s.
                await asyncio.sleep(SIGNUP_SPACING_SECONDS)
            email = account_email(run_id, i)

            async with session.post(
                f"{base_url}/auth/register", json={"email": email, "password": password}
            ) as response:
                if response.status != 201:
                    sys.exit(f"Could not register {email}: {response.status} {await response.text()}")

            async with session.post(
                f"{base_url}/auth/login", json={"email": email, "password": password}
            ) as response:
                if response.status != 200:
                    sys.exit(f"Could not log in as {email}: {response.status} {await response.text()}")
                token = (await response.json())["access_token"]

            async with session.get(
                f"{base_url}/orgs", headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status != 200:
                    sys.exit(f"Could not read organisations for {email}: {response.status} {await response.text()}")
                orgs = await response.json()
            personal = next((o for o in orgs if o.get("personal")), None)
            if personal is None:
                sys.exit(f"{email} has no personal organisation — cannot create its load-test bot.")
            org_id = personal["id"]

            async with session.post(
                f"{base_url}/bots/",
                headers={"Authorization": f"Bearer {token}", "X-Org-Id": org_id},
                json={
                    "name": f"Load test {i}",
                    "system_prompt": "You are a load test. Answer briefly.",
                    "language": "en",
                },
            ) as response:
                if response.status != 201:
                    sys.exit(f"Could not create a bot for {email}: {response.status} {await response.text()}")
                bot_id = (await response.json())["id"]

            accounts.append({"email": email, "bot_id": bot_id, "org_id": org_id})
            print(f"  {email} -> bot {bot_id} (org {org_id})")

    out.write_text(json.dumps(accounts, indent=2), encoding="utf-8")
    print(f"\n{len(accounts)} accounts written to {out}")
    print("Delete them afterwards with: python -m scripts.loadtest_accounts delete --yes (on the server)")


# ---------------------------------------------------------------------------
# delete — on the server, where the database login can remove a user
# ---------------------------------------------------------------------------


async def delete(confirmed: bool) -> None:
    from app.db.mongo import init_db
    from app.models.bot import Bot
    from app.models.conversation import ConversationTurn
    from app.models.organisation import Membership, Organisation
    from app.models.registry import ALL_MODELS
    from app.models.user import User

    # Task 5.1 — the shared model list, so this script's init_db stays in
    # step with what the app actually registers instead of a fourth
    # hand-maintained copy.
    await init_db(ALL_MODELS)

    users = await User.find_all().to_list()
    bots = await Bot.find_all().to_list()
    memberships = await Membership.find_all().to_list()
    organisations = await Organisation.find_all().to_list()
    targets = loadtest_targets(users, bots, memberships, organisations)
    if not targets.user_ids:
        print("No load-test accounts found.")
        return

    # Counted before anything is removed, so the summary printed at the end
    # describes what was actually there rather than what is left.
    turns = await ConversationTurn.find(
        {"bot_id": {"$in": targets.bot_ids}}
    ).count() if targets.bot_ids else 0

    print(
        f"{len(targets.user_ids)} load-test account(s), {len(targets.bot_ids)} bot(s), "
        f"{len(targets.membership_ids)} membership(s), {len(targets.org_ids)} now-empty organisation(s), "
        f"{turns} saved turn(s):"
    )
    for user in users:
        if str(user.id) in set(targets.user_ids):
            print(f"  {user.email}")

    if not confirmed:
        print("\nNothing deleted. Re-run with --yes to remove them.")
        return

    if targets.bot_ids:
        await ConversationTurn.find({"bot_id": {"$in": targets.bot_ids}}).delete()
    for membership in memberships:
        if str(membership.id) in set(targets.membership_ids):
            await membership.delete()
    for user in users:
        if str(user.id) in set(targets.user_ids):
            await user.delete()
    for bot in bots:
        if str(bot.id) in set(targets.bot_ids):
            await bot.delete()
    # After the memberships that held them open are gone, not before —
    # loadtest_targets already decided this from that same "after" view.
    for org in organisations:
        if str(org.id) in set(targets.org_ids):
            await org.delete()
    print(
        f"\nDeleted {len(targets.user_ids)} account(s), {len(targets.bot_ids)} bot(s), "
        f"{len(targets.membership_ids)} membership(s), {len(targets.org_ids)} organisation(s) and "
        f"{turns} turn(s)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("create", help="register the accounts over the public API")
    make.add_argument("--base-url", required=True)
    make.add_argument("--count", type=int, required=True)
    make.add_argument("--out", type=Path, default=Path("loadtest_accounts.json"))

    remove = sub.add_parser("delete", help="remove them (run on the server)")
    remove.add_argument("--yes", action="store_true", help="actually delete, rather than just listing")

    args = parser.parse_args()
    if args.command == "create":
        asyncio.run(create(args.base_url.rstrip("/"), args.count, args.out))
    else:
        asyncio.run(delete(args.yes))


if __name__ == "__main__":
    main()
