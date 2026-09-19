# Task 5.1 — Organisations, teams and roles: design

Date: 2026-09-19 · Branch: `phase-4-integration` · Status: awaiting user review

## Goal

Bots and everything attached to them belong to an **organisation**, not to a single
user. Several people can share an organisation, each with a role that limits what
they can do. Done when (manual 5.1): two organisations exist, each sees only its own
data, and roles genuinely restrict what people can do.

## Decisions already made with the user

| Question | Decision |
|---|---|
| One person in several organisations? | **Yes, with a switcher.** Memberships are many-to-many. |
| Roles | owner > admin > member > viewer, per the table in Part 2. |
| Existing accounts and new sign-ups | **Everyone gets a personal organisation** ("<name>'s workspace") as owner, and all their data moves into it. New sign-ups get one automatically. |
| Adding people in 5.1 | **Add existing Voix users by email**, change roles, remove. Invitation emails for people without an account are 5.2. |
| Enforcement approach | **A:** one organisation check declared on every route, plus tests that catch any route or model that misses it. (B, silent query filtering, was rejected: it gives false confidence because fetch-by-id bypasses it. C, a database per organisation, is Phase 8.) |

## Non-goals (explicitly later)

- Invitation emails and accept/decline — **5.2**.
- Deleting an organisation that still has data — needs cascading deletion across
  MongoDB, GridFS and Pinecone, which is **6.5**. In 5.1 only an *empty* organisation
  can be deleted.
- Company login / SSO — **6.6**. Usage metering, plans, billing — **5.3–5.5**.
- Moving a bot between organisations. `org_id` is immutable once set.
- Merging the `phase-6` branch. Its three new models are caught by the model test
  (Part 4) when it merges.

---

## Part 1 — Data and migration

### New collections

- **`organisations`**: `name`, `created_by` (user id), `personal: bool`,
  `owner_count: int`, `created_at`. `owner_count` exists so the "always at least one
  owner" rule can be enforced with one atomic single-document update (see Part 2);
  transactions are not relied on because the Atlas M0 docs do not confirm support.
- **`memberships`**: `org_id`, `user_id`, `role`, `created_at`. Unique index on
  `(org_id, user_id)`; index on `user_id` (for "my organisations").

### `org_id` on every tenant record

The server sets `org_id`; it is never accepted from a request body.

| Collection | Where `org_id` comes from |
|---|---|
| `bots` | the checked organisation at creation |
| `documents` | the bot's organisation |
| `webhook_subscriptions` | the checked organisation |
| `bot_tools` | the bot's organisation |
| `conversation_turns` | the bot's organisation (call pipeline) |
| `appointments` | the bot's organisation (booking) |
| `payment_sessions` | the bot's organisation (tool registry) |
| `pending_approvals` | the bot's organisation (tool registry) |
| GridFS `fs.files` (uploaded PDFs) | `metadata.org_id`, from the document's bot |

`user_id` stays on records as *who created it* and is no longer used for access.

Left alone: `users`, `revoked_refresh_tokens`, `webhook_deliveries` and
`webhook_outbox` (reached through their subscription), Pinecone (already namespaced
per bot), and `orders`. `orders` is Phase 1 demo data shared by every bot; it is
flagged, not migrated.

### Indexes, `org_id` first

`bots(org_id)`, `documents(org_id, bot_id)`,
`conversation_turns(org_id, bot_id, created_at)`,
`appointments(org_id, bot_id, starts_at_utc)`,
`pending_approvals(org_id, status, created_at)`,
`webhook_subscriptions(org_id, event, enabled)`, `payment_sessions(org_id)`,
`bot_tools(org_id, bot_id)`.

### Migration script — `backend/scripts/migrate_orgs.py`

- `--dry-run` prints what would change and changes nothing; the default run applies.
- Idempotent and resumable: it only touches records that still have no `org_id`.
- Steps:
  1. Every user with no membership gets a personal organisation
     (`owner_count=1`) and an owner membership.
  2. Bots, documents, webhook subscriptions and approvals take their owner's
     (`user_id`) personal organisation.
  3. Bot tools, turns, appointments, payment sessions and GridFS files take their
     bot's `org_id`.
- Records whose bot or owner no longer exists are **not guessed**. They are listed
  and left without `org_id`, which makes them invisible (the safe default).
- Batched with `update_many` per bot / per owner, keeping well under M0's
  100 operations/second.
- Ends by printing the count of records still missing `org_id`, by collection.

**Self-healing:** registration creates user → organisation → membership. If a step
fails half-way, the next login finds the user has no membership and creates the
personal organisation there. No transaction is needed for sign-up.

---

## Part 2 — The organisation check and roles

### How a request names its organisation

Every org-scoped request sends an **`X-Org-Id`** header. The frontend takes it from
the page address (Part 3), so each browser tab has its own. The access token does
**not** carry the organisation. Membership is read from the database on every request
(one indexed lookup), so a removed member or changed role takes effect on their very
next request, with no token revocation.

### Dependencies (`backend/app/core/org.py`)

- `get_org_context(x_org_id, user)` → `OrgContext(user, org_id, role)`:
  - header missing → **400** "Choose an organisation"
  - no such membership (including an organisation that doesn't exist) → **404**
    "Organisation not found"
- `require_role(minimum)` → dependency factory: below the minimum → **403** "Your role
  (<role>) can't do this". The member already knows the organisation exists, so 403
  leaks nothing.
- Resource fetchers look the resource up **by id AND org_id in one query**:
  `fetch_org_bot`, `fetch_org_document`, `fetch_org_tool`, `fetch_org_approval`,
  `fetch_org_webhook`. Not found in this organisation → **404**, identical to "doesn't
  exist", as `deps.py` does today.
- `deps.py`'s `get_owned_bot` / `fetch_owned_bot` / `get_owned_document` are
  **replaced**, not kept alongside, so no route can keep using the per-user check.

### Role table (approved)

| Action | Owner | Admin | Member | Viewer |
|---|---|---|---|---|
| Talk to a bot | ✓ | ✓ | ✓ | ✓ |
| See bots, docs, tools, transcripts | ✓ | ✓ | ✓ | ✓ |
| Create / edit / delete bots, docs, tools | ✓ | ✓ | ✓ | ✗ |
| Approve / deny big actions (3.10), see approvals | ✓ | ✓ | ✗ | ✗ |
| Webhooks and their secrets | ✓ | ✓ | ✗ | ✗ |
| Add / remove members, change roles | ✓ | ✓* | ✗ | ✗ |
| Rename organisation | ✓ | ✓ | ✗ | ✗ |
| Delete organisation (empty only in 5.1) | ✓ | ✗ | ✗ | ✗ |
| Billing (5.5) | ✓ | ✗ | ✗ | ✗ |

\* An admin cannot add, remove, promote to or demote an owner. An organisation always
keeps at least one owner. Anyone may leave an organisation, except its last owner.

### Route → minimum role

| Route | Minimum |
|---|---|
| `GET /bots`, `GET /bots/{id}`, `GET /bots/{id}/tools`, `GET /bots/{id}/documents`, `GET /documents/{id}/file` | viewer |
| `POST /connect` (the bot must be in the organisation) | viewer |
| `POST/PATCH/DELETE` on bots, tools, documents; `POST /bots/{id}/tools/{id}/test` | member |
| `/approvals/*` (count, list, approve, deny) | admin |
| `/webhooks/*` (except `GET /webhooks/events`), including `/_debug/outbox-summary` | admin |
| `GET /orgs/{id}/members` | viewer |
| `POST/PATCH/DELETE /orgs/{id}/members…` | admin (owner rules above); leaving yourself: any role |
| `PATCH /orgs/{id}` (rename) | admin |
| `DELETE /orgs/{id}` | owner; the organisation must have no bots, and it must not be your last one |

**Exempt**, meaning not org-scoped, each with a reason in the coverage test:
`/auth/*`; `GET /orgs` and `POST /orgs` (list mine, create new, any logged-in user);
`GET /bots/templates` (static); `GET /webhooks/events` (static);
`GET /connect/ice-servers` (per user, no tenant data); `POST /connect/ice` (a live call belongs to a *person*: it gets a per-user check instead, see below); `POST /payments/webhook/{tool_id}`
(provider, HMAC-verified, carries `org_id` from the payment session);
`/health`, `/health/detail`, `/metrics` (token), `/test`.

### Owner-count rule without transactions

Removing or demoting an owner first runs
`organisations.find_one_and_update({_id, owner_count: {$gt: 1}}, {$inc: {owner_count: -1}})`.
If that matches nothing, it is the last owner → **409** "An organisation needs at
least one owner". If the membership change then fails, the count is incremented back.
Promoting to owner increments it. Two admins acting at the same moment cannot both
pass, because the conditional decrement is atomic on one document.

### Organisation and member endpoints (`backend/app/api/orgs.py`)

- `GET /orgs` — my organisations, with my role in each.
- `POST /orgs` — create one (`name`); I become its owner.
- `PATCH /orgs/{id}` — rename.
- `DELETE /orgs/{id}` — empty organisations only.
- `GET /orgs/{id}/members` — email, role, joined date.
- `POST /orgs/{id}/members` — `{email, role}`. The user must already have an account.
  "No Voix account uses that email" does tell an *admin* whether an address is
  registered; accepted for 5.1, rate-limited to 10/minute, and replaced by uniform
  "invitation sent" wording in 5.2.
- `PATCH /orgs/{id}/members/{user_id}` — change role.
- `DELETE /orgs/{id}/members/{user_id}` — remove, or leave when it is yourself.

Removing a member never deletes what they built: the organisation owns it.

### During a call

- `/connect` resolves the bot inside the checked organisation, and passes
  `org_id = bot.org_id` to the call worker alongside today's fields.
- `CallContext` gains `org_id`.
- Booking, the tool registry (payments, approvals) and the pipeline (turns) stamp it.
- `webhooks.emit()` looks up subscriptions by `org_id` instead of the owner's
  `user_id`.
- The rule "one live call per **person**" (`_active_calls` keyed by `user_id`) is
  deliberately unchanged. It is about a human with one microphone, not about an
  organisation.

---

### Pre-existing gap fixed here

`POST /connect/ice` (`connect.py:675`) feeds ICE candidates into whatever call matches
the `pc_id` in the body, without checking that the call belongs to the caller. `pc_id`
values are hard to guess, but that is an object-level authorization gap (OWASP API1)
all the same. 5.1 adds the check: an unknown `pc_id` or one belonging to another user
is ignored exactly as an ended call is today, so no information leaks.

## Part 3 — Screens

- **Addresses carry the organisation:** `/o/:orgId/dashboard`, `/o/:orgId/bots/:id`,
  `/o/:orgId/bots/:id/tools`, `/o/:orgId/session/:id`, `/o/:orgId/webhooks`,
  `/o/:orgId/approvals`, plus new `/o/:orgId/members` and `/o/:orgId/settings`.
- **Old addresses still work.** `/dashboard`, `/bots/:id` etc. redirect to the same
  page under the last-used organisation (localStorage, per-viewer convenience only),
  falling back to the personal one. Bookmarks survive.
- **Per-tab organisation.** An `OrgProvider` reads `:orgId` from the address and hands
  it to the API client in memory. The client adds `X-Org-Id` to every request. Nothing
  global, so two tabs can be in two organisations safely.
- **An organisation in the address that I'm not in** → a "not found — choose an
  organisation" page listing mine.
- **Switcher** in the AppShell header: current organisation name; a dropdown of mine
  with my role in each; "Create organisation".
- **Members page:** list; add by email with a role; change role; remove; leave.
  Controls a role can't use are hidden. The server remains the authority.
- **Settings page:** rename; delete (owner only, empty only, with the reason shown
  when it isn't allowed).
- **Role-aware UI:** viewers see no create/edit/delete buttons. Only admins and owners
  fetch the approvals badge count and see the Webhooks and Approvals links.
- **A live call survives switching.** The call bar stays, and its link back goes to
  `/o/<the call's org>/session/...`.

---

## Part 4 — Testing and deploy

### Tests (backend pytest locally against the test database only; never in production)

1. **Route coverage:** walk `app.routes`. Every `APIRoute` must depend on
   `get_org_context`, or be on the exempt list with a written reason. A new route
   without the check fails CI.
2. **Model coverage:** every Beanie `Document` model registered in `init_db` must
   have an `org_id` field or be on the exempt list with a reason. This catches the
   `phase-6` models at merge time.
3. **Two-organisation isolation** (OWASP API1): organisations A and B, each with a
   bot, document, tool, webhook and approval. For every org-scoped route, a member of A
   using B's ids — with A's header, and with B's header — gets 404. A's own requests
   succeed.
4. **Role matrix:** parametrised over role × action from the Part 2 table, both
   allowed and refused.
5. **Owner-count rule:** last owner cannot leave, be removed or be demoted; admin
   cannot touch owners; two concurrent demotions of the last two owners leave exactly
   one.
6. **Migration script:** dry run changes nothing; a real run fills everything;
   running it twice is a no-op; orphans are reported and left untagged; GridFS
   metadata is stamped.
7. **Registration and self-heal:** sign-up creates a personal organisation; a user
   with no membership gets one on login.
8. **Call path:** a call started through `/connect` stamps `org_id` on turns,
   appointments, payments and approvals; webhooks fire for the bot's organisation.
9. **Frontend (vitest):** the client sends `X-Org-Id` from the address; old addresses
   redirect; the switcher lists organisations; controls are hidden by role.

Then a local click-through with two accounts: one organisation, owner plus viewer.

### Deploy (production rule: backup, say what changes, go-ahead)

1. `docker compose run --rm backup python -m scripts.db_backup snapshot`
   (not the default `loop` command). Tag the current backend image `pre-5.1`.
2. Migration `--dry-run` against production; show the user the counts.
3. User go-ahead → run the migration. It only *adds* fields, so the running old code
   is unaffected.
4. Deploy the backend and rebuild the frontend.
5. Run the migration again, catching records created between steps 3 and 4.
6. Verify read-only: logs, counts of records missing `org_id` (expect only orphans),
   `/health`, a real call.

**Rollback:** the previous image (`pre-5.1`) and the previous frontend build. Old code
ignores `org_id`, and `user_id` is kept, so it keeps working, including on records
created by the new code. The snapshot is only needed if data itself goes wrong.

## Risks and notes

- **Size:** the manual's 8h is optimistic. Every route, every tenant writer and every
  frontend page changes. Expect several sessions.
- **`phase-6` merge:** its models (`knowledge_sources`, `consent_records`,
  `guardrail_incidents`) and its retention job must become org-aware when merged. The
  model coverage test enforces it.
- **`/health/detail`** shows the operator's view (database, breakers, pool) to *any*
  logged-in user. Harmless with one customer; with several it belongs behind the 5.7
  admin role. Noted, not changed in 5.1.
- **Email enumeration** on "add member" is accepted for 5.1 (admins only,
  rate-limited). It is fixed by 5.2's invitation wording.
