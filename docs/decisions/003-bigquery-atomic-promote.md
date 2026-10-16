# 003: BigQuery loads are staged, then promoted in one transaction

## Context
The local warehouse commits data and watermark in one database transaction. BigQuery has no
connection-level transaction, and its free sandbox does not allow DML (no MERGE) at all.

## Decision
- Use a project with billing enabled (still inside the free tier at this data volume).
- `commit()` loads the extracted rows into a throwaway staging table with a load job, then runs
  ONE script: `BEGIN TRANSACTION; MERGE raw table; MERGE _sync_state; COMMIT`, with a handler that
  rolls back and re-raises on any error. The staging table is always dropped afterwards.
- Both warehouse targets expose the same `commit(resource, records, watermark)`, so the extractor
  is identical for local and BigQuery.

## Why
- Data and watermark still move together: a failure leaves both untouched, so the next run re-covers
  the same window. The safety property does not depend on the engine.
- Load jobs are free and atomic; one MERGE per resource per run is cheap and keeps DML counts low.

## Trade-offs
- A failed run can leave an orphaned staging table if the process is killed before cleanup. They are
  prefixed `_stg_` and are safe to delete.
- Billing must be enabled, which means a card on file. A budget alert guards against surprises.
- The BigQuery path is tested here with a stand-in client (order of operations and SQL), and for real
  by running against a project; there is no BigQuery in CI.
