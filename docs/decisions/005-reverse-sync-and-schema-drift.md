# 005: Reverse sync that is safe to run twice, and schema drift that cannot pass silently

## Context
Two integration problems remain after the load is safe. The warehouse computes things sales needs
(account segment, whether usage is falling), and they are only useful in the CRM. And because raw
records are stored as JSON, a CRM schema change never breaks the load, which also means it can go
unnoticed.

## Decision: reverse sync
- Read the computed fields from the marts; read the CRM's **current** values live; send only the
  accounts whose values differ, in batches. Running it twice sends nothing the second time.
- **Dry run is the default.** `--apply` is explicit. A cap (`--max-changes`) refuses an unexpectedly
  large change set, and every applied change is appended to an audit log (old value, new value, time).
- Missing values are never written, so an account with no usage data keeps no `usage_trend`.
- The CRM stamps `updated_at` only when a value really changes, so re-sending the same value is a no-op.
- The written fields are deliberately **not read back by dbt staging**. The marts must not depend on
  their own output.

## Decision: schema drift
- A committed snapshot (`extract/schema_snapshot.json`) records the fields and value types we expect.
  Every sync compares what it fetched with it and reports new fields, fields missing from every
  record, and type changes.
- Drift is **reported, not blocked**: the load goes ahead, because raw JSON cannot break and holding
  data back would be worse. `--fail-on-drift` turns it into a failing exit code for CI.
- Accepting a change is deliberate: review it, run `python -m extract.schema --update`, commit the diff.
- The fields the warehouse writes back are marked optional, since they exist only after a reverse sync.

## Why
- Comparing against the live CRM, not the warehouse copy, means the plan is right even when the last
  sync is stale. A failed batch needs no cleanup: the next run recomputes the difference.
- Batches that fail halfway leave earlier batches applied, and that is fine, because every write is
  "set this field to this value" and therefore safe to repeat.
- A schema change shows up as a message on the day it happens, not as a wrong number weeks later.

## Trade-offs
- The reverse sync is not atomic across batches. It is idempotent instead, which is simpler and just as safe here.
- Comparing against the live CRM costs a full read of the accounts on every run. Fine at this size.
- The snapshot is a file in the repo, so changing it is a reviewable commit, but it has to be kept up
  to date by hand. A test fails if the mock CRM's fields change without it.
- The write path targets the mock CRM's batch endpoint, modeled on HubSpot's batch update. Real
  HubSpot has its own limits, authentication and property setup.
