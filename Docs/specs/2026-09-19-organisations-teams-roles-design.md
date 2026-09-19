# Task 5.1 — Organisations, teams and roles: design

Date: 2026-09-19 · Branch: `phase-4-integration` · Status: revised after independent
review (4 critical, 9 important, 8 minor findings, all addressed below); awaiting user
review

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
  MongoDB, GridFS and Pinecone, which is **6.5**. See "Deleting an organisation" in
  Part 2 for what 5.1 allows.
- Company login / SSO — **6.6**. Usage metering, plans, billing — **5.3–5.5**.
- Moving a bot between organisations. `org_id` is immutable once set.
- Merging the `phase-6` branch. Its three new models are caught by the model test
  (Part 4) when it merges.

---

## Part 1 — Data and migration

### New collections

- **`organisations`**: `name`, `created_by` (user id), `personal: bool`,
  `owner_count: int`, `created_at`.
  - **Unique partial index on `created_by` where `personal: true`**, so a user can
    never have two personal organisations, whatever races happen (review C2).
  - `owner_count` backs the "always at least one owner" rule without transactions
    (Part 2). The Atlas M0 docs do not confirm transaction support.
- **`memberships`**: `org_id`, `user_id`, `role`, `created_at`. Unique index on
  `(org_id, user_id)`; index on `user_id` (for "my organisations").

### Getting a personal organisation (one function, used everywhere)

`ensure_personal_org(user)` is the only code that creates a personal organisation.
Registration, the self-heal, and migration step 1 all call it.

1. `organisations.find_one_and_update({created_by: user, personal: true},
   {$setOnInsert: {...name, owner_count: 1}}, upsert=True)`. The unique partial index
   turns a concurrent duplicate into a retry-and-read.
2. Then upsert the owner membership `{org_id, user_id}`.

Running it twice, or from two places at once, always leaves exactly one personal
organisation with one owner membership.

### `org_id` on every tenant record

The server sets `org_id`; it is never accepted from a request body.

| Collection | Where `org_id` comes from at write time | Migration source |
|---|---|---|
| `bots` | the checked organisation | owner's personal org (via `user_id`) |
| `webhook_subscriptions` | the checked organisation | owner's personal org (via `user_id`) |
| `documents` | the bot | the bot |
| `bot_tools` | the bot | the bot |
| `conversation_turns` | the bot (call pipeline) | the bot |
| `appointments` | the bot (booking) | the bot |
| `booking_slots` (raw collection, `booking.py:207`) | the bot | the bot |
| `payment_sessions` | **the tool** (`tool.org_id`), not the call context (review I1) | the bot, else the tool |
| `pending_approvals` | **the tool** (`tool.org_id`) | the bot, else the tool |
| GridFS `fs.files` (uploaded PDFs) | `metadata.org_id`, from the document's bot | the bot, via `metadata.bot_id` |

Payment sessions and approvals take the tool's organisation because two of their
writers run *outside* a call, in the API process, where the call context is empty:
the tool Test button (`tool_registry.py` `test_tool`) and approve (`call_http_tool`
from `approvals.py`).

`user_id` stays on records as *who created it* and is no longer used for access.

Left alone: `users`, `revoked_refresh_tokens`, `webhook_deliveries` and
`webhook_outbox` (reached only through their subscription), Pinecone (already
namespaced per bot), and `orders`. `orders` is Phase 1 demo data shared by every bot;
it is flagged, not migrated.

### Indexes, `org_id` first

`bots(org_id)`, `documents(org_id, bot_id)`, `bot_tools(org_id, bot_id)`,
`conversation_turns(org_id, bot_id, created_at)`,
`appointments(org_id, bot_id, starts_at_utc)`,
`pending_approvals(org_id, status, created_at)`,
`webhook_subscriptions(org_id, event, enabled)`, `payment_sessions(org_id)`.

### Migration script — `backend/scripts/migrate_orgs.py`

- `--dry-run` prints what would change and changes nothing; the default run applies.
- Idempotent and resumable: it only touches records that still have no `org_id`.
- Steps:
  1. `ensure_personal_org` for every user. This finds orgs made by registration or
     self-heal and never makes a second one.
  2. Bots and webhook subscriptions take their owner's personal org.
  3. Everything bot-owned (documents, tools, turns, appointments, `booking_slots`,
     GridFS files) takes its bot's `org_id`. Payment sessions and approvals do too,
     falling back to their tool's `org_id` when `bot_id` is blank.
  4. Recount `owner_count` for every organisation from its memberships, and correct
     any drift.
- **Expected orphans**, listed rather than guessed and left without `org_id`, which
  makes them invisible:
  - turns with `bot_id = None`;
  - pre-3.5 appointments with `bot_id = ""`;
  - sessions and approvals whose bot *and* tool are gone;
  - anything whose bot or owner no longer exists.
- GridFS files are stamped with a filter on `metadata.bot_id` and
  "`metadata.org_id` absent", so reruns are no-ops.
- Batched with `update_many` per bot / per owner, keeping well under M0's
  100 operations/second.
- Ends by printing the count of records still missing `org_id`, by collection.

**Self-healing** happens in `GET /orgs`, the first call the frontend makes after
login *or* after a silent token refresh (review I5): if the user has no membership at
all, it calls `ensure_personal_org`. Registration calls it too, so a half-finished
sign-up is repaired on first use.

---

## Part 2 — The organisation check and roles

### How a request names its organisation

- **Org-scoped routes** (bots, tools, documents, calls, approvals, webhooks) read the
  **`X-Org-Id`** header. The frontend takes it from the page address (Part 3), so each
  tab has its own.
- **Routes that name an organisation in their path** (`/orgs/{org_id}/…`) take the
  organisation from the **path**. If an `X-Org-Id` header is also present it must equal
  the path, otherwise 404. This closes a privilege escalation where an admin of A sends
  `X-Org-Id: A` to act on `/orgs/B` (review C1).
- The access token does **not** carry the organisation. Membership is read from the
  database on every request (one indexed lookup), so a removed member or changed role
  takes effect on their very next request.

### Dependencies (`backend/app/core/org.py`)

Checks run in this order, so the frontend's refresh-on-401 keeps working (review I3):

1. `get_current_user` → an expired or missing token is **401**.
2. The organisation id, read with `Header(default=None)` (or from the path):
   - missing → **400** "Choose an organisation" (not FastAPI's automatic 422);
   - not a valid ObjectId → **404**.
3. Membership lookup → none (including an organisation that doesn't exist) → **404**
   "Organisation not found".
4. `require_role(minimum)` → below it → **403** "Your role (<role>) can't do this".

Two dependencies share that logic: `get_org_context` (header) and
`get_org_context_from_path` (path). Both return `OrgContext(user, org_id, role)`.

Resource fetchers look the resource up **by id AND org_id in one query** —
`fetch_org_bot`, `fetch_org_document`, `fetch_org_tool`, `fetch_org_approval`,
`fetch_org_webhook`. Not found in this organisation → **404**, identical to "doesn't
exist". `deps.py`'s per-user helpers (`get_owned_bot`, `fetch_owned_bot`,
`get_owned_document`) are **removed**, not kept alongside.

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
| Delete organisation (see below) | ✓ | ✗ | ✗ | ✗ |
| Billing (5.5) | ✓ | ✗ | ✗ | ✗ |

\* An admin cannot add, remove, promote to or demote an owner. An organisation always
keeps at least one owner. Anyone may leave an organisation, except its last owner.

### Route → minimum role (method + full path, as mounted)

| Route | Minimum |
|---|---|
| `GET /bots/`, `GET /bots/{bot_id}/tools/`, `GET /bots/{bot_id}/documents`, `GET /documents/{doc_id}/file` | viewer |
| `POST /connect` (the bot must be in the organisation) | viewer |
| `POST /bots/`, `PATCH`/`DELETE /bots/{bot_id}`; `POST /bots/{bot_id}/tools/`, `PATCH`/`DELETE …/tools/{tool_id}`, `POST …/tools/{tool_id}/test`; `POST /bots/{bot_id}/documents`, `DELETE /documents/{doc_id}` | member |
| `GET /approvals/pending-count`, `GET /approvals/`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/deny` | admin |
| `GET/POST /webhooks/`, `PATCH`/`DELETE /webhooks/{sub_id}`, `POST /webhooks/{sub_id}/test`, `GET /webhooks/{sub_id}/deliveries`, `GET /webhooks/_debug/outbox-summary` | admin |
| `GET /orgs/{org_id}/members` | viewer (from path) |
| `POST /orgs/{org_id}/members`, `PATCH`/`DELETE /orgs/{org_id}/members/{user_id}` | admin (owner rules below); `DELETE` on yourself (leave): any role |
| `PATCH /orgs/{org_id}` (rename) | admin (from path) |
| `DELETE /orgs/{org_id}` | owner (from path) |

**Exempt**, meaning not org-scoped; each is listed with a reason in the coverage test:

- `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`
- `GET /orgs`, `POST /orgs` (list mine, create new; any logged-in user)
- `GET /bots/templates`, `GET /webhooks/events` (static)
- `GET /connect/ice-servers` (per user, no tenant data)
- `POST /connect/ice` (per-user check, see "Pre-existing gaps")
- `POST /payments/webhook/{tool_id}` (provider, HMAC-verified)
- `GET /health`, `GET /health/detail`, `GET /metrics` (token), `GET /test`

Also: `approvals.approve` fetches the tool with `BotTool.get(approval.tool_id)`. It
must also require `tool.org_id == approval.org_id`.

### Owner-count rule without transactions (review C3)

Every failure leaves `owner_count` *low*, which is the safe direction: it can refuse a
legitimate change, never allow zero owners.

- **Demote or remove an owner:**
  1. Conditional decrement: `{_id, owner_count: {$gt: 1}}` → `$inc -1`. If nothing
     matches, it's the last owner → **409**.
  2. Conditional membership write, matching `{org_id, user_id, role: "owner"}`.
  3. If that matches nothing (someone else already demoted them), re-increment.
- **Promote to owner:** conditional membership write `{…, role: {$ne: "owner"}}`
  first; increment only if it modified a document.
- **Add a new member as owner:** insert the membership first; increment only on
  success. A duplicate-key error means already a member → **409**.
- The migration's step 4 recount corrects any drift. A test checks that the count
  equals the real number of owners after concurrent operations.

### Organisation and member endpoints (`backend/app/api/orgs.py`)

- `GET /orgs` — my organisations with my role in each; runs the self-heal.
- `POST /orgs` — create one (`name`); I become its owner. Rate-limited 10/minute.
- `PATCH /orgs/{org_id}` — rename (personal organisations can be renamed).
- `DELETE /orgs/{org_id}` — see below.
- `GET /orgs/{org_id}/members` — email, role, joined date.
- `POST /orgs/{org_id}/members` — `{email, role}`. The email is normalised the same
  way as the users index. The user must already have an account. "No Voix account
  uses that email" tells an *admin* whether an address is registered; accepted for
  5.1, rate-limited 10/minute, and replaced by uniform "invitation sent" wording in 5.2.
- `PATCH /orgs/{org_id}/members/{user_id}` — change role.
- `DELETE /orgs/{org_id}/members/{user_id}` — remove, or leave when it is yourself.

Removing a member never deletes what they built: the organisation owns it. A personal
organisation is an ordinary organisation. It can be shared, renamed or left like any
other; `personal` only records where it came from. A user left with **zero**
memberships (for example, an added owner removed the original owner from their
personal org) gets a fresh personal organisation from the self-heal in `GET /orgs`.
If a personal org they created already exists and they are no longer in it, the new
one is created by upserting on `(created_by, personal: true)` after marking the old
one `personal: false`.

### Deleting an organisation (review I4)

Allowed only when the organisation is **empty**: no bots, no webhook subscriptions and
no pending approvals. It must also not be your last organisation, since the self-heal
would only recreate it.

Known leftovers from bots deleted earlier (`DELETE /bots/{id}` does not cascade):
tools, documents, GridFS files, turns, appointments and `booking_slots`. These keep
their `org_id` and are on the **6.5 cascade list**, with nothing new orphaned by 5.1.

### During a call (review I2)

- `/connect` resolves the bot inside the checked organisation and adds
  `org_id = bot.org_id` to the `bot_config` dict. That dict travels through
  `job_queue` for the warm pool, or through `Process` args for a cold spawn.
- `run_voice_pipeline(**bot_config)` (`call_worker.py:301`) must gain an `org_id`
  parameter, otherwise every call raises `TypeError` after the SDP answer. That error
  is swallowed by `logger.exception("pipeline crashed")`, so the call goes silent.
  It passes `org_id` to `call_context.set_call` (`voice_pipeline.py:470`).
- The backend `app/pipeline/call_context.py` `CallContext` gains `org_id`. The
  frontend `CallContext.tsx` is a different thing, covered in Part 3.
- Booking and the pipeline (turns) stamp `org_id` from the call context. Payment
  sessions and approvals stamp it from the tool (see Part 1).
- `webhooks.emit()` takes `org_id` instead of `user_id`, and **all four callers**
  change:
  - `booking.py:692`
  - `voice_pipeline.py:846`
  - `approvals.py:225`, from `approval.org_id`
  - `payments.py:89`, from `session.org_id`
- The rule "one live call per **person**" (`_active_calls` keyed by the caller's
  `user_id`) is deliberately unchanged.

### Pre-existing gaps fixed here

- **C4 — cross-tenant appointment changes (serious).** `booking.py:503`
  `_find_booking(reference, bot_id)` ignores `bot_id` and searches every appointment.
  Any caller of any bot who says a live reference code can cancel or reschedule
  someone else's booking. The fix is to filter by `bot_id` (and `org_id`). **This does
  not depend on organisations and is recommended as an immediate standalone fix,
  before 5.1**, with its own test.
- **`POST /connect/ice`** (`connect.py:675`) feeds ICE candidates into whatever call
  matches `pc_id`, without checking that the call is the caller's. The fix adds
  `_active_calls[pc_id].user_id == current_user.id`. A mismatch is ignored exactly as
  an ended call is, so nothing leaks.

---

## Part 3 — Screens

- **Addresses carry the organisation.** `/o/:orgId/dashboard`,
  `/o/:orgId/bots/:id` (including `new`), `/o/:orgId/bots/:id/tools`,
  `/o/:orgId/session/:id`, `/o/:orgId/webhooks`, `/o/:orgId/approvals`, plus new
  `/o/:orgId/members` and `/o/:orgId/settings`. `AppShell` renders under `/o/:orgId`,
  so the switcher and the approvals badge polling see the organisation.
- **Old addresses still work.** `/dashboard`, `/bots/:id`, `/bots/new`,
  `/bots/:id/tools`, `/session/:id`, `/webhooks` and `/approvals` redirect to the same
  page under the last-used organisation (localStorage, a per-viewer convenience only),
  falling back to the personal one. Login lands on `/o/<that org>/dashboard`.
- **One org-aware path helper** (`orgPath('/bots/…')`) replaces every hard-coded
  path. The review found them in `AppShell.tsx` (83, 119, 145, 174),
  `BotSettingsPage.tsx` (103, 247, 364, 541), `BotToolsPage.tsx` (60),
  `DashboardPage.tsx` (32, 55, 83, 93), `SessionPage.tsx` (27, 173),
  `ApprovalsPage.tsx` (22), `WebhooksPage.tsx` (23), `LoginPage.tsx` (29) and
  `CallBar.tsx` (24, 118).
- **Per-tab organisation, and every request carries it.** An `OrgProvider` reads
  `:orgId` from the address and hands it to the API client in memory. `request()`
  adds `X-Org-Id`. So do the two functions that build their own headers:
  `uploadDocument` (`api.ts:357`) and `fetchDocumentBlobUrl` (`api.ts:386`).
- **A live call keeps its own organisation.** `CallProvider` sits above the routes, so
  it records the organisation at `startCall` and uses it for `connectBot` and for
  opening citations (`openSource` → `fetchDocumentBlobUrl`). A citation opened after
  switching organisation still works. The call bar hides on its own session page and
  links back with the call's organisation.
- **Errors:** "organisation not found" is decided client-side from `GET /orgs`, not
  from a 404. An organisation in the address that isn't in my list shows a "choose an
  organisation" page. `DashboardPage`'s `.catch(() => navigate('/'))` becomes
  "go to the chooser", so a 400/403/404 no longer drops the user on the login page.
  A 403 shows its message.
- **Zero organisations** (only possible if the self-heal failed) shows a short
  "setting up your workspace — retry" state, not a blank page.
- **Switcher** in the AppShell header: current organisation name; a dropdown of mine
  with my role in each; "Create organisation".
- **Members page:** list; add by email with a role; change role; remove; leave.
  Controls a role can't use are hidden. The server remains the authority.
- **Settings page:** rename; delete (owner only; shows why when not allowed).
- **Role-aware UI:** viewers see no create/edit/delete buttons. Only admins and owners
  fetch the approvals badge count and see the Webhooks and Approvals links.

---

## Part 4 — Testing and deploy

### Tests (backend pytest locally against the test database only; never in production)

1. **Route coverage:** walk `app.routes`, recursing through
   `route.dependant.dependencies[*].call`. Every `APIRoute` must reach
   `get_org_context` or `get_org_context_from_path`, or be on the exempt list, keyed by
   method + full path with a written reason. A new route without the check fails.
2. **Model coverage:** one `ALL_MODELS` constant, used by `main.py`,
   `call_worker.py`, `conftest.py` and `loadtest_accounts.py` instead of today's four
   separate lists. Every model in it must have `org_id` or be exempt with a reason
   (`User`, `RevokedRefreshToken`, `Organisation`, `Membership`, `Order`,
   `WebhookDelivery`, `WebhookOutboxItem`). Raw collections (`booking_slots`, GridFS)
   are covered by migration tests, since the model test cannot see them.
3. **Two-organisation isolation** (OWASP API1): organisations A and B, each with a
   bot, document, tool, webhook and approval. For every org-scoped route, a member of
   A using B's ids gets 404, whether they send A's header or B's. For every
   `/orgs/{org_id}` route, an admin of A acting on B — including with `X-Org-Id: A` —
   gets 404. A's own requests succeed.
4. **Role matrix:** parametrised over role × action from the Part 2 table, both
   allowed and refused.
5. **Owner-count rule:**
   - the last owner cannot leave, be removed or be demoted;
   - an admin cannot touch owners;
   - concurrent demote/demote, promote/promote and add-as-owner leave `owner_count`
     equal to the real owner count;
   - the recount corrects deliberately introduced drift.
6. **Personal org uniqueness:** concurrent `ensure_personal_org` calls
   (registration + self-heal + migration) leave exactly one personal organisation.
7. **Migration script:**
   - a dry run changes nothing; a real run fills everything; running it twice is a
     no-op;
   - orphans are reported and left untagged;
   - the tool fallback works for blank `bot_id`;
   - GridFS and `booking_slots` are stamped.
8. **Status codes:** missing header → 400 (not 422); malformed id → 404; expired
   token → 401 before any org check; wrong role → 403.
9. **Call path, through a real worker:**
   - a call started through `/connect` stamps `org_id` on turns and appointments;
   - payment sessions and approvals get the tool's org, including from the Test button
     and approve outside a call;
   - `emit` fires for the bot's organisation from all four callers.
10. **Booking isolation (C4):** a reference from bot X cannot cancel or reschedule
    through bot Y.
11. **Frontend (vitest):**
    - the client sends `X-Org-Id` from the address, including upload and blob fetch;
    - old addresses redirect;
    - the call keeps its organisation after switching;
    - the switcher lists organisations;
    - controls are hidden by role;
    - errors route to the chooser.
12. **Existing suites updated** to the org helpers and header (review I9):
    `test_ownership`, `test_approvals`, `test_webhooks`, `test_bots_list_fields`,
    `test_one_call_per_user`, `test_connect_validates_before_ending`,
    `test_loadtest_accounts`, `test_payment_link`.

**Load-test scripts** (review I7): `scripts/loadtest_accounts.py` looks up each
account's organisation via `GET /orgs` and stores it in the accounts JSON. Its
`delete` also removes the accounts' organisations and memberships.
`scripts/load_test.py` sends `X-Org-Id` on `/connect`.

Then a local click-through with two accounts: one organisation, owner plus viewer.

### Deploy (production rule: backup, say what changes, go-ahead)

1. `docker compose run --rm backup python -m scripts.db_backup snapshot`
   (not the default `loop` command). Tag the current backend image `pre-5.1`.
2. Migration `--dry-run` against production; show the user the counts.
3. User go-ahead → run the migration. It only *adds* fields and collections, so the
   running old code is unaffected.
4. Deploy the backend and rebuild the frontend.
5. Run the migration again. `ensure_personal_org` reuses the orgs made by the new code
   in between, and the recount runs.
6. Verify read-only: logs, counts of records missing `org_id` (expect only the listed
   orphans), `/health`, a real call.

**Rollback:** the `pre-5.1` image and the previous frontend build. Old code ignores
`org_id` and still has `user_id`, so it runs, but *degraded*:
- anything a teammate created in a shared organisation becomes visible only to its
  creator;
- webhooks an admin set up for someone else's bot stop firing, because the old code
  looks them up by the bot owner's `user_id`.

With only the user's own accounts in use today, that impact is small. The snapshot is
only needed if data itself goes wrong.

## Risks and notes

- **Size:** the manual's 8h is optimistic. Every route, every tenant writer, the call
  worker, the load-test scripts, eight existing test suites and every frontend page
  change. Expect several sessions.
- **`phase-6` merge:** its models (`knowledge_sources`, `consent_records`,
  `guardrail_incidents`) and its retention job must become org-aware when merged. The
  model coverage test enforces it.
- **`/health/detail`** shows the operator's view (database, breakers, pool) to *any*
  logged-in user. Harmless with one customer; with several it belongs behind the 5.7
  admin role. Noted, not changed in 5.1.
- **Email enumeration** on "add member" is accepted for 5.1 (admins only,
  rate-limited) and fixed by 5.2's invitation wording.
