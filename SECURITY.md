# Security overview

Task 6.7's own tip: write this while it is fresh, so there is a clear
two-page answer ready rather than something to reconstruct under pressure
during a deal. Everything below describes what the code in this repository
actually does, as of Phase 6 — not a target state, not a claim about
infrastructure this document cannot see from inside the repo.

Reporting a vulnerability: open a private GitHub security advisory on this
repository, or contact the maintainer directly. Please do not open a public
issue for a suspected vulnerability.

## Authentication and sessions

- Passwords are hashed with bcrypt (`passlib`'s `CryptContext`), never
  stored or logged in plain form.
- Access tokens are short-lived JWTs (15 minutes). A stolen token is live
  for minutes, not hours.
- Refresh tokens (7 days) are revocable — logging out invalidates the
  specific token, not just the client's copy of it — via a revocation list
  checked on every refresh.
- `/auth/register` and `/auth/login` are rate-limited (5/minute) against
  credential-stuffing and account-creation abuse; `/auth/refresh` is
  limited separately (20/minute).
- Every record (bots, tools, documents, conversations, webhooks,
  approvals) is scoped to the authenticated user's own `user_id` at the
  query level, not filtered after the fact — verified directly in
  `tests/test_ownership.py`.

## Data in transit and at rest

- Deployment is HTTPS-only (Caddy, automatic Let's Encrypt certificates).
  Browsers refuse microphone access on a non-HTTPS origin regardless, so
  this is enforced by the platform as well as by policy.
- Customer-supplied credentials this system must be able to USE later
  (a tool's API key, a webhook's signing secret, a payment provider's
  webhook secret) are encrypted at rest with Fernet
  (`backend/app/core/crypto.py`), keyed from `SECRET_KEY` — a database
  dump alone does not yield them. The UI only ever shows a masked form
  (last four characters).
  - Stated limit, not hidden: the encryption key is derived from
    `SECRET_KEY`, so it sits behind the same trust boundary as JWT
    signing already does. Raising that bar means a managed KMS (AWS KMS,
    GCP KMS) — appropriate at a later scale, over-engineered for one VM
    today. Rotating `SECRET_KEY` currently makes existing stored
    credentials undecryptable; there is no re-encryption-in-place path
    yet.
- **Encryption at rest for the database and file storage is a platform
  setting this repository cannot enforce or verify from inside the code**
  — it is confirmed on the MongoDB Atlas cluster's own configuration
  (on by default for every current Atlas tier, including the free/shared
  ones this project uses, but the operator should confirm this directly
  in the Atlas console rather than take a document's word for it).
  Uploaded knowledge-base files live in MongoDB GridFS — the same
  database, not a separate object store — so this is the one setting
  that covers both.

## Protecting this server from what it is asked to fetch

Two features make this server issue an outbound HTTP request to an address
someone else configured: a bot tool's URL (task 3.1) and a webhook
subscription's URL (task 3.8). Both are checked against
`backend/app/core/url_safety.py` immediately before every request — not
only when saved — because a name that resolved publicly at save time can
be re-pointed at `127.0.0.1` afterwards (DNS rebinding).

- Loopback, private ranges, link-local, multicast and reserved blocks are
  all refused. Link-local specifically matters because this is a GCP VM:
  `169.254.169.254` is the cloud metadata service, and it hands out the
  instance's own service-account token to anything on the box that asks.
- A redirect is followed only after the SAME check runs again on the
  target — a customer endpoint answering `302 -> 169.254.169.254` is
  refused at that hop, not just the first one.
- A redirect across hosts drops any credential that was attached for the
  original host, the way a browser would.
- Only GET/HEAD are followed across a redirect; a POST is never replayed,
  so a redirect cannot cause a booking or a charge to happen twice.
- `{placeholder}` values from a caller's own words are percent-encoded
  before reaching a URL, and a rendered path containing `..` is refused —
  a caller cannot redirect a configured endpoint to a different one on
  the same host by what they say.

## The voice channel itself (task 6.1)

A caller is the least trusted input this system has — anonymous, over
audio, transcribed by a third party. Every bot carries the same
unconditional instructions regardless of what it is told to do
mid-call: never reveal its own prompt, never claim an unconfirmed action
succeeded, never give medical/legal/financial advice, never be hostile,
never deny being an AI — phrased to survive a claimed authority, a claimed
rule change, or a roleplay framing. Known jailbreak phrasing in what a
caller says is logged for review; the bot's own reply is checked one
sentence at a time and a leak or a forbidden-topic mention is replaced
before it ever reaches speech synthesis.

Stated limit: detection (as opposed to the always-on prompt instructions)
is English-only today, and a single check has no memory across
conversational turns — see `backend/app/core/guardrails.py`'s own
docstring for the full reasoning.

## Data minimisation

- Card numbers, CVVs, government ID numbers, phone numbers and email
  addresses are masked out of a call's transcript before it is ever
  written to the database — including numbers spoken aloud rather than
  typed, and including inside a tool's own stored arguments/result (task
  6.2, `backend/app/core/redaction.py`). This runs whether the number
  came from the caller or was read back by the bot.
- Callers are told a call may be recorded, with per-customer wording and
  an on/off switch; a bot can be configured to delete its own transcripts
  automatically after a set number of days (task 6.3).
- Payment is never taken by voice: a bot is instructed to never ask for
  or repeat card details, and to send a payment link instead
  (`PAYMENT_SAFETY_RULE`, task 3.7).

## Automated scanning (this repository's CI, `.github/workflows/ci.yml`)

- **Secret scanning** — gitleaks runs on every push, checking the full
  history of each push for committed credentials.
- **Dependency vulnerability scanning** — `pip-audit` runs against
  `backend/requirements.txt` on every push.
- **Dependency version updates** — Dependabot (`.github/dependabot.yml`)
  opens a weekly pull request for outdated Python, npm, Docker base image,
  and GitHub Actions dependencies. This is deliberately separate from
  pip-audit: pip-audit catches a vulnerability disclosed against a
  dependency already pinned; Dependabot is what proposes moving the pin
  forward in the first place, on a schedule, whether or not a CVE has
  been filed yet.
- **Prompt-injection pattern blocking** — a bot's own configured system
  prompt is checked at save time for known override phrasing
  (`backend/app/api/bots.py`), separate from and in addition to the
  runtime guardrails above that watch what a *caller* says mid-call.

## Approval gating for high-value actions

A tool can be configured with a value threshold above which it pauses for
a human decision rather than completing unattended (task 3.10) — the
manual's own reasoning applies directly: no company will let an AI approve
a large refund without a person in the loop, and this is what makes it
safe to let it handle everything below that threshold on its own.

## Known, stated limits

Written here rather than left to be discovered, because a security
document that only lists what is covered is not trustworthy about what
isn't:

- `SECRET_KEY` rotation currently breaks every stored credential (tool
  keys, webhook secrets) with no re-encryption path.
- Guardrail input detection (task 6.1) is English-only; the always-on
  prompt instructions are not, but a Hindi-phrased attack will not
  additionally appear in the audit log the way an English one does.
- A single guardrail check has no memory across conversational turns, so
  an attack built up gradually across several innocuous-seeming exchanges
  is not caught by design.
- `pip-audit` in CI is currently informational
  (`continue-on-error: true`) rather than blocking — see that job's own
  comment in `ci.yml` for the condition under which it should be flipped
  to a hard gate.
- This document describes the application layer. It does not, and cannot
  from inside a code repository, describe or guarantee the security
  posture of the underlying cloud VM, its OS patching, or the MongoDB
  Atlas cluster's own access controls — those are the operator's
  responsibility to configure and review directly.
