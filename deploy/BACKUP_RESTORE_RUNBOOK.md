# Backup and restore runbook

Task 6.8's own framing: *"an untested backup is not a backup. You find out
whether it works on the day you desperately need it, which is the worst
possible time to find out."*

**This document cannot complete task 6.8 by itself.** Steps 1-4 below are
Atlas UI actions and a real timed restore that only the account holder,
with access to the Atlas dashboard and billing, can actually perform — the
manual is explicit that reading this is not the same thing as having done
it. What this runbook provides is the exact procedure to follow and the
tool (`scripts/verify_restore.py`, task 6.8's own step 3/4) to objectively
confirm a restore worked rather than trusting Atlas's "restore complete"
message on faith.

## Before you start

Confirm which Atlas tier this project is actually on. Continuous Cloud
Backup (point-in-time restore) is available from the M10 tier upward;
the free/shared tiers (M0/M2/M5) instead offer **snapshot-based backups**
with a coarser schedule. The steps below work for either — a snapshot
restore is simply "restore to the most recent snapshot" rather than
"restore to any point in time" — but confirm which one you actually have
before promising a recovery point objective to anyone.

## Step 1 — confirm automatic backups are running

1. Atlas dashboard → your cluster → **Backup** tab.
2. Confirm backups are **enabled** (they are on by default on paid tiers;
   free/shared tiers may need this turned on explicitly — check).
3. Note the actual schedule shown (e.g. "snapshot every 6 hours, retained
   7 days" or similar) — write down the REAL numbers here, not an
   assumption:

   ```
   Backup type:        ______________________
   Snapshot frequency: ______________________
   Retention period:   ______________________
   Confirmed on (date): _____________________
   ```

## Step 2 — set retention to match your actual commitment

If this project has told any customer (in `SECURITY.md`, in a contract, or
verbally) how long their data is retained, the Atlas backup retention
setting has to be at least that long, or a restore requested near the end
of that window could find nothing to restore from. Set it under **Backup**
→ **Edit Backup Schedule**, and record what you set:

```
Retention set to: __________________ (date changed: __________)
```

## Step 3 — actually perform a restore (do not skip this)

**To a NEW, separate test cluster or a local deployment — never restore
over the live database to test this.**

1. Atlas dashboard → **Backup** → pick a recent snapshot → **Restore**.
2. Choose **"Restore to a different cluster"** — create a small, temporary
   test cluster for this (M0 free tier is fine for this purpose).
3. Start the restore and **note the start time**.
4. When Atlas reports it complete, **note the end time**. This is the real
   number the manual asks for — write it down, because "restores take
   about 20 minutes" from documentation is not the same fact as "it took
   34 minutes for OUR data on OUR tier," and only the second one is worth
   planning an incident response around.

```
Snapshot restored from: ____________________ (timestamp of the snapshot)
Restore started:        ____________________
Restore completed:      ____________________
Total time:             ____________________
```

## Step 4 — verify the restore actually has real data, not just a status

Get the connection string for the restored test cluster (Atlas dashboard
→ that cluster → **Connect**), then run:

```bash
cd backend
python -m scripts.verify_restore --url "mongodb+srv://.../test-restore" --db voiceagent
```

This checks that every collection this project's models actually use is
present, and reports a document count for each — not because a count
alone proves the DATA is correct, but because Atlas's own "restore
complete" message describes the JOB succeeding, not what is actually in
the result. The manual's own examples of what goes wrong are specific:
"a missing permission, a wrong region, an incomplete snapshot" — every one
of those can leave Atlas reporting success on a restore that is not
actually usable, and only checking the target directly tells the two
apart.

Read the script's output. `NOT OK` and a list of missing collections means
exactly what it says: stop, and find out why before trusting this restore
path.

```
Verification run on: ________________
Result (OK / NOT OK): _______________
Collections missing, if any: ________
```

## Step 5 — tear down the test cluster

Once verified, delete the temporary test cluster — leaving it running is
an unnecessary ongoing cost, and per this project's own standing rule,
nothing here should be creating recurring spend without it being a
deliberate decision.

## When to repeat this drill

- After any change to which Beanie models exist (a new collection means
  `scripts/verify_restore.py`'s `EXPECTED_COLLECTIONS` needs updating too
  — `tests/test_verify_restore.py::test_expected_collections_matches_every_real_model`
  fails in CI if it drifts, so this is enforced automatically rather than
  relying on remembering to do it).
- At least once before onboarding the first paying customer whose data
  this project would be responsible for recovering.
- Periodically thereafter (quarterly is a reasonable starting cadence) —
  an Atlas tier change, a plan downgrade, or an account issue can silently
  change what backups actually exist, and the only way to know is to
  check again.
