# CRM Analytics Pipeline for an EdTech SaaS Company

An end-to-end analytics pipeline built around a simulated CRM: incremental extraction from a REST API, safe reruns, a BigQuery warehouse, dbt models with field-level definitions, automated data quality checks, a reverse sync back to the CRM, and a Streamlit dashboard. Everything runs on a schedule via GitHub Actions.

> **Status: work in progress.** Steps 1-2 of 7 are complete and tested: the mock CRM API, the incremental extractor, and the local warehouse. Sections marked *(planned)* describe the intended design and are not implemented yet.
>
> **Requirements:** Python 3.10+ (developed and tested on 3.12).

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

Implemented so far: mock CRM -> extractor -> local warehouse (raw tables and sync state). Everything else in the diagram is planned.

## Scope

| Module | What it does | Skill shown | Status |
|---|---|---|---|
| Mock CRM API | FastAPI app serving accounts, contacts, deals, campaigns, usage; supports pagination, `updated_since`, injected failures | Understanding CRM data structures and API behavior | Done |
| Extractor | Pulls changed records only, retries transient errors, writes raw tables | Python, REST extraction, incremental loads | Done |
| Warehouse | Local DuckDB (done); BigQuery with MERGE (planned) | Loading, MERGE, SQL | Local done, BigQuery planned |
| dbt project | Staging, intermediate, and mart models with documented fields | Data modeling, metric definitions | Planned |
| Data quality | dbt tests, source-to-warehouse reconciliation, schema drift detection | Trustworthy numbers | Planned |
| Reverse sync | Writes computed fields back to the CRM | Bidirectional integration | Planned |
| Dashboard | Funnel, channel conversion, usage trends, pipeline health | Delivering to business users | Planned |
| Automation | GitHub Actions: tests and end-to-end smoke test on every push (done); scheduled sync (planned) | Scheduling, basic CI/CD | CI done, schedule planned |

## Key design: incremental sync and safe reruns

The extractor is designed so that running it twice never produces wrong data.

- **Watermark per object.** `_sync_state` stores the last successful `updated_at` high-water mark for each object type.
- **Incremental pulls.** First run fetches everything; later runs request only records with `updated_at` after the watermark (minus a small overlap window to catch late writes).
- **Idempotent loads.** Records land in a staging table and are applied with `MERGE` on the primary key (e.g. `deal_id`). Re-running updates existing rows instead of inserting duplicates.
- **Watermark advances only after success.** If a page fails or the load fails, the watermark stays where it was, so the next run picks up the same window.
- **Retries with backoff.** Transient errors (429/5xx/timeouts) are retried; permanent errors fail the run loudly.
- **Failure injection.** The mock API can fail on demand (`FAIL_RATE`, `FAIL_ON_PAGE`) so this behavior is demonstrated, not just claimed.

## Data model *(planned)*

Raw layer: `raw_accounts`, `raw_contacts`, `raw_deals`, `raw_deal_stage_history`, `raw_campaigns`, `raw_usage_events`.

| Layer | Model | Purpose |
|---|---|---|
| Staging | `stg_*` | Rename, type-cast, deduplicate; no business logic |
| Intermediate | `int_deal_stage_durations` | Days spent in each stage per deal |
| Marts | `dim_accounts` | One row per institution, with segment and `buying_motion` |
| Marts | `dim_contacts` | Contacts with role (professor / instructional designer) |
| Marts | `fct_deals` | One row per deal with source campaign, stage, amount, cycle length |
| Marts | `fct_campaign_performance` | Leads, qualified deals, win rate, and cost per channel |
| Marts | `fct_account_usage_weekly` | Weekly active users and sessions per institution |
| Marts | `account_health` | Usage trend plus engagement score used for follow-up |

Every column in the marts has a definition in `schema.yml`, and `docs/data_dictionary.md` is generated from them. Example definition: a *qualified deal* is a deal that reached the "Discovery Completed" stage or later; the rule lives in one place.

Attribution: first-touch and last-touch campaign attribution are both modeled so the two can be compared.

## Data quality *(planned)*

Checks run on every scheduled run and on every pull request.

- **dbt tests**: primary keys unique and not null; deals reference valid accounts; stage values in an accepted list; amounts greater than zero; close date not before create date.
- **Source reconciliation**: record counts and total deal amount in the warehouse are compared against the API for the same window. A mismatch fails the run.
- **Schema drift detection**: the extractor compares incoming fields against the expected schema and alerts on new, missing, or retyped fields instead of silently dropping data.
- **Freshness**: the dashboard shows when each source last synced successfully.
- **Alerting**: failures open a GitHub issue (or post to a Slack webhook if configured).

## Reverse sync *(planned)*

The warehouse is not a dead end. Computed fields are written back to the CRM so sales sees them where they work.

- Fields: `account_segment`, `engagement_score`, `usage_trend`.
- **Dry-run by default**; a flag enables real writes.
- Only rows whose values changed are sent, in batches.
- Each run logs what was changed, so any write can be traced.

## Dashboard *(planned)*

Streamlit app with four pages:

1. **Funnel**: deals by stage and conversion between stages, split by buying motion.
2. **Channels**: qualified deals and win rate by marketing channel.
3. **Customer usage**: weekly usage trend per institution, with at-risk accounts flagged.
4. **Pipeline health**: last sync times, test results, and reconciliation status.

## Repository layout

Folders for later steps (`dbt_project/`, `quality/`, `reverse_sync/`, `dashboard/`) currently hold placeholders.

```
.
├── mock_crm_api/          # FastAPI mock CRM + synthetic data generator
├── extract/               # incremental extractor, retry logic, watermark state
├── load/                  # BigQuery loaders (MERGE) and DuckDB test target
├── dbt_project/           # models, tests, schema.yml definitions
├── quality/               # reconciliation and schema drift checks
├── reverse_sync/          # warehouse -> CRM writer (dry-run by default)
├── dashboard/             # Streamlit app
├── tests/                 # unit tests for extractor, watermark, retry, MERGE
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

The local warehouse is DuckDB (`warehouse.duckdb`). The BigQuery target, dbt, quality checks, reverse sync, and dashboard are added in later steps.

## Design decisions

- **Hand-written extractor instead of a managed connector.** A tool like Airbyte is a good default where a connector exists. Writing the extractor here shows what such tools do for you (cursoring, retries, state, schema handling) and how I would operate a custom pipeline when no connector fits.
- **Mock API instead of a real CRM account.** It makes the demo reproducible and lets me inject failures and schema changes on demand.
- **Watermark with an overlap window plus MERGE.** Simple and robust: late-arriving updates are caught, and overlap never creates duplicates.
- **Reverse sync is dry-run by default.** Writing to a system that sales uses daily deserves a guardrail.
- **Reconcile before publishing.** A silently wrong number is worse than a missing one, so the pipeline fails loudly on mismatch.

## Limitations

- Data is synthetic, so business findings are illustrative.
- The mock API approximates HubSpot-style behavior; real API rate limits, auth flows, and edge cases will differ.
- Health scoring is rule-based and meant as a starting point, not a trained model.
- Gong, billing, and other sources are out of scope for this version; the extractor pattern is intended to extend to them.

## Status

- [x] Mock CRM API and synthetic data
- [x] Incremental extractor with retries and watermark
- [x] Unit tests and CI (GitHub Actions on every push)
- [ ] BigQuery load with MERGE
- [ ] dbt staging and marts with definitions
- [ ] Data quality checks and schema drift detection
- [ ] Reverse sync
- [ ] Streamlit dashboard
- [ ] Scheduled sync workflow
- [ ] Architecture diagram and short demo recording

## Author

Zoe (Zexu) Chen: Data Analyst. Built as a portfolio project for analytics engineering roles.
