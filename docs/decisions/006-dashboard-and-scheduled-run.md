# 006: A dashboard that only displays, and a scheduled run that catches silent breakage

## Context
The marts answer the business questions, but only someone who can run SQL can read them. And a
pipeline that passes its checks on the day it is written can still break later without anyone
touching the code: a new library release, a changed API, a check that starts failing.

## Decision: dashboard
- Every query lives in `dashboard/queries.py` and returns plain lists of dicts. The Streamlit file
  only lays them out. **The dashboard never computes a business metric**; it reads the marts, so a
  definition changes once, in dbt.
- It opens the warehouse **read-only** and closes it after reading, so it never blocks a sync or a
  dbt build on DuckDB (which allows one writer).
- A **Pipeline health** page shows data freshness, the result of the last dbt build, and the last
  reconciliation, next to the numbers, so a reader can tell whether to trust them.
- If the marts are missing it says what to run, instead of showing a stack trace.

## Decision: scheduled run
- A GitHub Actions workflow runs the whole pipeline on weekdays: seed the mock CRM, sync (failing on
  schema drift), reconcile, `dbt build`, reverse sync as a dry run. Reports are saved as artifacts.
- On failure it opens an issue, or comments on the one already open, so failures are not silent
  and do not pile up.

## Why
- The query layer can be tested without a browser; the filter and funnel math are covered by unit
  tests and were checked against figures computed independently from the raw data.
- Showing pipeline health next to the metrics makes "is this wrong?" answerable on the same screen.
- A schedule finds breakage that no commit caused, which is the failure that otherwise goes unseen.

## Trade-offs
- The scheduled run proves the pipeline works end to end on synthetic data and a throwaway DuckDB
  file. It does not sync a real warehouse. A BigQuery version needs credentials in the repository
  (Workload Identity Federation is the safer option than a key file) and is a deliberate later step.
- Scheduled workflows run only on the default branch, and GitHub pauses them after a stretch of
  repository inactivity.
- The dashboard has no authentication and is meant to run locally or behind a company login.
- Data is cached for 60 seconds, so a number can be up to a minute behind the warehouse.
