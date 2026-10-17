# CRM Analytics Pipeline for an EdTech SaaS Company

An end-to-end analytics pipeline built around a simulated CRM: incremental extraction from a REST API, safe reruns, a BigQuery warehouse, dbt models with field-level definitions, automated data quality checks, a reverse sync back to the CRM, and a Streamlit dashboard. Everything runs on a schedule via GitHub Actions.

> **Status: work in progress.** Steps 1-6 of 7 are complete and tested: the mock CRM API, the incremental extractor, DuckDB and BigQuery warehouses, the dbt staging, intermediate and mart layers, row-level reconciliation, schema-drift detection, a reverse sync back to the CRM, a Streamlit dashboard, and a scheduled pipeline run. What remains is the architecture diagram and a short demo recording.
>
> **Requirements:** Python 3.10+ (developed and tested on 3.12). The optional BigQuery target needs a Google Cloud project with billing enabled; the free BigQuery sandbox does not support MERGE.

> All data is synthetic. The CRM is a local mock API that mimics the shape and behavior of HubSpot-style endpoints (pagination, `updated_at` filtering, rate limits, occasional failures).

## Business context

I play the role of the data analyst at an AI-for-education company that sells to universities. The sales team has three questions today that nobody can answer reliably:

1. **Which marketing channels produce the most qualified deals?**
2. **At which sales stage do deals stay the longest?**
3. **Which institutions have declining product usage and need follow-up?**

The company sells through two buying motions, so the data model keeps them separate:

- **Professor-led adoption**: an individual instructor adopts the product for their courses.
- **Instructional-designer-led adoption**: a campus-level team adopts it across programs.

## Architecture

```
 Mock CRM REST API            Python extractor              BigQuery
 (accounts, contacts,   --->  incremental, paginated,  --->  raw_* tables
  deals, campaigns,           retry + backoff,               + _sync_state
  product usage)              watermark state                      |
        ^                                                          v
        |                                                   dbt (staging -> marts)
        |                                                   tests + definitions
        |                                                          |
        |   PATCH (dry-run by default)                             v
        +----------------------------------------  Reverse sync   Streamlit dashboard
                                                   (segments, scores)  + data quality page

 GitHub Actions: scheduled run, manual trigger, CI on pull requests
```

Implemented so far: mock CRM -> extractor -> warehouse (DuckDB or BigQuery) -> dbt (staging, intermediate, marts), plus reconciliation, schema-drift detection, the reverse sync, the dashboard and a scheduled run. Everything in the diagram is implemented.

## Scope

| Module | What it does | Skill shown | Status |
|---|---|---|---|
| Mock CRM API | FastAPI app serving accounts, contacts, deals, campaigns, usage; supports pagination, `updated_since`, injected failures | Understanding CRM data structures and API behavior | Done |
| Extractor | Pulls changed records only, retries transient errors, writes raw tables | Python, REST extraction, incremental loads | Done |
| Warehouse | Local DuckDB and BigQuery; BigQuery loads are staged, then promoted with MERGE in one transaction | Loading, MERGE, SQL | Done |
| dbt project | Staging, intermediate and mart models, with business rules in one seed and a few variables | Data modeling, metric definitions | Done |
| Data quality | dbt tests, source-to-warehouse reconciliation (row by row), schema-drift detection | Trustworthy numbers | Done |
| Reverse sync | Writes computed fields back to the CRM: dry run by default, only changed rows, audit log | Bidirectional integration | Done |
| Dashboard | Streamlit: sales funnel, channel performance, customer usage, and pipeline health | Python dashboard, business delivery | Done |
| Automation | GitHub Actions: tests and an end-to-end smoke test on every push; a scheduled weekday run that opens an issue on failure | Scheduling, basic CI/CD | Done |

## Key design: incremental sync and safe reruns

The extractor is designed so that running it twice never produces wrong data.

- **Watermark per object.** `_sync_state` stores the last successful `updated_at` high-water mark for each object type.
- **Incremental pulls.** First run fetches everything; later runs request only records with `updated_at` after the watermark (minus a small overlap window to catch late writes).
- **Idempotent loads.** Records land in a staging table and are applied with `MERGE` on the primary key (e.g. `deal_id`). Re-running updates existing rows instead of inserting duplicates.
- **Watermark advances only after success.** If a page fails or the load fails, the watermark stays where it was, so the next run picks up the same window.
- **Retries with backoff.** Transient errors (429/5xx/timeouts) are retried; permanent errors fail the run loudly.
- **Failure injection.** The mock API can fail on demand (`FAIL_RATE`, `FAIL_ON_PAGE`) so this behavior is demonstrated, not just claimed.

## Data model

Raw layer: `raw_accounts`, `raw_contacts`, `raw_deals`, `raw_campaigns`, `raw_usage`. Each row holds the record's `id`, `updated_at`, the full record as JSON in `payload`, and the load time. A deal's stage history is a JSON array inside its payload.

| Layer | Model | Purpose |
|---|---|---|
| Seed | `stage_definitions` | The pipeline stages in order, and which are closed, won, or make a deal "qualified". The one place these definitions live |
| Staging | `stg_*` | Rename, type-cast, parse the JSON; no business logic |
| Intermediate | `int_deal_stage_history` | One row per stage a deal has entered, unpacked from the JSON array, with days spent in each |
| Marts | `fct_deals` | One row per deal: channel, buying motion, outcome and qualification flags, cycle length |
| Marts | `fct_channel_performance` | Per channel: deals, qualified, won, win rate, spend, cost per qualified and per won deal |
| Marts | `fct_stage_durations` | Average days in each stage per buying motion, ranked to show where deals wait longest |
| Marts | `dim_accounts` | One row per institution: size segment, deal history, customer status, contact mix by role |
| Marts | `account_health` | Per customer institution: recent vs prior weekly usage, trend, and a follow-up flag |

The marts answer the three business questions: which channels bring qualified deals (`fct_channel_performance`), where deals stay longest (`fct_stage_durations`), and which customers are using the product less (`account_health`).

Every column has a definition in the `_*.yml` files next to the models. Business rules sit in `seeds/stage_definitions.csv` and in the variables in `dbt_project.yml` (usage decline threshold, size bands), so a definition changes in one place.

Attribution is **single-touch**: a deal is credited to its source campaign. Multi-touch attribution needs touch-level data the CRM does not provide here.

## Data quality

Implemented:

- **dbt tests (over 70 across the layers)**: unique and non-null keys, accepted values, relationships between tables, and custom rules: amounts are positive, deals cannot close before they are created, a deal's current stage must match the last entry in its own stage history, channel rows must add up to all deals, every deal must have a stage history, and a stage the CRM adds without a definition fails loudly.
- **Contract test (Python)** between the raw JSON payloads and the staging models: fails when a staging model reads a field the CRM does not have, or the CRM has a field nobody staged.
- **Source reconciliation** (`python -m quality.reconcile`): re-reads the whole CRM and compares it with the warehouse id by id, reporting rows that are *missing* (sync is behind), *stale* (older copy), or *deleted at the source*. It exits non-zero on any difference. This catches what incremental sync cannot: records deleted in the CRM.

- **Schema-drift detection** on every sync: the fetched records are compared with a committed snapshot of the expected fields and types (`extract/schema_snapshot.json`). New fields, fields missing from every record, and type changes are reported. The load still goes ahead, since raw JSON cannot break; `--fail-on-drift` makes it a failing exit code. Accepting a change is deliberate: `python -m extract.schema --update`, then commit the diff.

The checks also run on a schedule (see *Scheduled run* below).

## Reverse sync

The warehouse is not a dead end. The marts compute fields that sales cares about, and the reverse sync writes them back to the CRM as custom fields on the account: `account_segment` (size band), `usage_trend` (declining, stable, growing) and `needs_follow_up`.

- **Dry run by default.** `python -m reverse_sync.run` shows what would change and writes nothing; `--apply` sends it.
- **Compares against the CRM as it is right now**, and sends only accounts whose values differ. A second run sends nothing.
- **Batched and safe to interrupt.** If a batch fails, earlier batches stay applied and a rerun sends only what is still different.
- **A cap** (`--max-changes`) refuses an unexpectedly large change set.
- **Audit log** of every applied change: old value, new value, time.
- Missing values are never written, and the written fields are not read back by dbt staging, so the marts never depend on their own output.

See `docs/decisions/005-reverse-sync-and-schema-drift.md`.

## Dashboard

`make dashboard` starts a Streamlit app (`pip install -r requirements-dashboard.txt` first) with four tabs:

1. **Sales funnel**: deals, win rate and average days to close, how many deals reach each stage by buying motion, and where deals wait longest.
2. **Marketing channels**: qualified deals and cost per qualified deal by channel.
3. **Customer usage**: which customer institutions are using the product less, filterable by size, with a weekly usage chart per account.
4. **Pipeline health**: how fresh each table is, the result of the last dbt build, and the last reconciliation against the CRM.

Every number comes from the dbt marts; the app calculates no business metric itself. All queries live in `dashboard/queries.py` and are unit-tested without a browser. It reads the warehouse read-only, so it never blocks a sync or a dbt build. See `docs/decisions/006-dashboard-and-scheduled-run.md`.

## Scheduled run

`.github/workflows/scheduled.yml` runs the whole pipeline on weekdays (and on demand from the Actions tab): seed the mock CRM, sync (failing on schema drift), reconcile, `dbt build`, reverse sync as a dry run. Reports are saved as artifacts, and a failure opens a GitHub issue (or comments on the one already open). It catches breakage nobody committed, such as a new library release.

## Repository layout

```
.
├── mock_crm_api/          # FastAPI mock CRM + synthetic data generator
├── extract/               # incremental extractor, retry logic, watermark state
├── load/                  # warehouse targets: local DuckDB and BigQuery (MERGE)
├── dbt_project/           # dbt staging models, macros, tests, field definitions
├── quality/               # reconciliation check
├── reverse_sync/          # warehouse -> CRM writer (dry run by default, audit log)
├── dashboard/             # Streamlit app
├── tests/                 # unit tests: extractor, watermark, retries, warehouse targets, staging contract
├── docs/
│   ├── data_dictionary.md
│   └── decisions/         # short notes on key design choices
└── .github/workflows/     # scheduled run, manual dispatch, CI
```

## Running it locally

```bash
# 1. Environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Generate synthetic data and start the mock CRM (leave it running; docs at /docs)
python -m mock_crm_api.seed
uvicorn mock_crm_api.main:app --port 8000

# 3. In a second terminal: full load, then incremental runs
python -m extract.run                              # first run loads everything
python -m extract.run                              # re-reads only a small overlap, no duplicates
curl -X POST "localhost:8000/admin/mutate?n_updates=10&n_new=3"   # simulate CRM activity
python -m extract.run --resources deals            # pulls only the changes

# 4. Demonstrate failure handling (small pages so there is a page 2 to fail on)
curl -X POST localhost:8000/admin/failures -H 'content-type: application/json' \
     -d '{"fail_on_page": 2, "fail_times": 999}'
python -m extract.run --resources deals --page-size 3   # retries, then fails; watermark unchanged
curl -X POST localhost:8000/admin/failures -H 'content-type: application/json' -d '{}'
python -m extract.run --resources deals --page-size 3   # recovers; nothing was lost

# 5. Tests
python -m unittest discover -s tests -t . -v
```

The local warehouse is DuckDB (`warehouse.duckdb`).

**Build all dbt layers** (seeds, staging, intermediate, marts, and every test). Run it after a sync; DuckDB allows one process on the file at a time:

```bash
make dbt-build        # runs: cd dbt_project && dbt build --profiles-dir .

# Check the warehouse against the CRM, row by row (exit code 1 on any difference)
make reconcile

# Demo: delete 2 deals in the CRM, sync, and see that only reconciliation notices
make delete-demo && make sync && make reconcile

# Reverse sync: preview, then write the marts' computed fields back to the CRM (needs the dbt build)
make reverse-sync
make reverse-sync-apply
make reverse-sync            # second run: nothing left to update

# Demo: a write that fails halfway, then recovers
make write-fail-demo && make reverse-sync-apply     # first batch lands, then it fails
make clear-failures && make reverse-sync-apply      # sends only what is still missing

# Dashboard (install first: pip install -r requirements-dashboard.txt)
make dashboard

# Demo: the CRM changes its schema; the next sync reports it
make drift-demo && python -m extract.run --resources accounts   # prints SCHEMA DRIFT: new field region
```

**Optional: run on BigQuery.** Needs a Google Cloud project with billing enabled (the free tier covers this project's data volume):

```bash
pip install -r requirements-bigquery.txt
gcloud auth application-default login        # sign in once with the Google account that owns the project
export GCP_PROJECT=<your-project-id>
python -m extract.run --target bigquery      # creates the crm_raw dataset and loads it
make dbt-build-bq                            # builds the staging views in an "analytics" dataset
```

Quality checks, reverse sync, and the dashboard are added in later steps.

## Design decisions

- **Hand-written extractor instead of a managed connector.** A tool like Airbyte is a good default where a connector exists. Writing the extractor here shows what such tools do for you (cursoring, retries, state, schema handling) and how I would operate a custom pipeline when no connector fits.
- **Mock API instead of a real CRM account.** It makes the demo reproducible and lets me inject failures and schema changes on demand.
- **Watermark with an overlap window plus MERGE.** Simple and robust: late-arriving updates are caught, and overlap never creates duplicates.
- **Raw records stored as JSON, typed in dbt staging.** A CRM schema change can never break the load; it shows up as a failing contract test or dbt error in one known place (see `docs/decisions/002-raw-json-and-typed-staging.md`).
- **BigQuery loads are staged, then promoted atomically.** Rows go into a throwaway table, then one script merges them and the new watermark inside a transaction, so a failure leaves both untouched (see `docs/decisions/003-bigquery-atomic-promote.md`). It needs billing enabled, because the free sandbox has no DML.
- **Business rules in one place, and reconciliation against the full source.** Stage definitions live in a seed and thresholds in dbt variables; a row-level reconciliation catches deletes that incremental sync cannot see (see `docs/decisions/004-business-rules-and-reconciliation.md`).
- **Reverse sync is dry-run by default.** Writing to a system that sales uses daily deserves a guardrail.
- **Reconcile before publishing.** A silently wrong number is worse than a missing one, so the pipeline fails loudly on mismatch.

## Limitations

- Data is synthetic, so business findings are illustrative.
- The mock API approximates HubSpot-style behavior; real API rate limits, auth flows, and edge cases will differ.
- The BigQuery path is verified by running it against a real project, not in CI (no cloud credentials in CI). CI runs the same pipeline on DuckDB.
- The scheduled run uses the mock CRM and a throwaway DuckDB file; a scheduled BigQuery run needs credentials and is a later step. The dashboard has no authentication.
- The reverse sync targets the mock CRM's batch-update endpoint, modeled on HubSpot's. Real HubSpot adds its own authentication, rate limits and property setup.
- Health scoring is rule-based and meant as a starting point, not a trained model.
- Gong, billing, and other sources are out of scope for this version; the extractor pattern is intended to extend to them.

## Status

- [x] Mock CRM API and synthetic data
- [x] Incremental extractor with retries and watermark
- [x] Unit tests and CI (GitHub Actions on every push)
- [x] BigQuery load with atomic MERGE
- [x] dbt staging, intermediate and mart models with field definitions and tests
- [x] Source reconciliation check
- [x] Schema-drift detection
- [x] Reverse sync to the CRM
- [x] Streamlit dashboard
- [x] Scheduled pipeline run
- [ ] Architecture diagram and short demo recording

## Author

Zoe (Zexu) Chen: Data Analyst. Built as a portfolio project for analytics engineering roles.
