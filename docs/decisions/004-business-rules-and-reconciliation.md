# 004: Business rules in one place, and row-level reconciliation

## Context
Two things decide whether the numbers can be trusted: whether everyone means the same thing by
"qualified" or "declining", and whether the warehouse actually matches the CRM. Incremental sync
cannot answer the second question: it never sees records deleted at the source, and it cannot tell
that a run quietly fell behind.

## Decision
- **Definitions live in data, not scattered in SQL.** `seeds/stage_definitions.csv` says which
  stages are closed, won, or make a deal qualified. Thresholds (usage decline, account size bands)
  are variables in `dbt_project.yml`. Models read them; nothing hard-codes a stage name or a cutoff.
- **Unknown values fail loudly.** The stage join is a LEFT join, and a not-null test on `stage_order`
  fails if the CRM introduces a stage nobody defined, instead of silently dropping those deals.
- **Cross-checks between sources of the same fact.** A test compares a deal's current stage with the
  last entry of its own stage history; others check that channel rows add up to all deals.
- **`quality/reconcile.py` re-reads the whole CRM and compares it with the warehouse id by id**,
  reporting rows that are missing, stale, or deleted at the source. It exits non-zero on any
  difference, so a scheduled run or CI job fails instead of publishing a wrong number.

## Why
- Changing a business definition is a one-line change, reviewed in one place.
- A silently wrong number is worse than a missing one: failing the run is the safe default.
- Comparing `id -> updated_at` is cheap, needs no row contents, and catches deletes that the
  incremental design (see 001) is blind to.

## Trade-offs
- Reconciliation scans the full source, so it costs a full read. Fine at this size; at scale it
  would run less often than the sync, or compare counts and checksums per partition.
- It must run right after a sync: a CRM that changed in between legitimately shows as missing or stale.
- It reports deletions but does not act on them. Whether to prune the warehouse or soft-delete
  in staging is a decision for the data's owners, not something a job should do on its own.
