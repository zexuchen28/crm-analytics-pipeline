# 002: Keep raw records as JSON, type them in dbt staging

## Context
The CRM schema changes without warning: fields get added, renamed or retyped. A raw table with one
column per CRM field would make the load fail, or silently drop data, whenever that happens.

## Decision
- The raw layer stores `id`, `updated_at`, the **full record as JSON text** in `payload`, and `_ingested_at`.
- dbt **staging** models are the first place that knows field names and types. They only rename,
  cast and parse; no business logic.
- Cross-database differences (JSON extraction, timestamp parsing) sit in a small set of macros so the
  same models run on DuckDB locally and BigQuery later.
- A Python test (`tests/test_staging_contract.py`) fails when staging reads a field the CRM does not
  have, or when the CRM has a field that no staging model reads.

## Why
- Loading can never break because of a CRM schema change. The extractor stays simple and stable.
- A schema change shows up as a failing test or a dbt error, in one known place, instead of as bad data.
- Field-level definitions are written once, next to the models (`_staging.yml`).

## Trade-offs
- Parsing cost moves downstream. For this data volume that does not matter; at larger volume the
  staging views would become incremental tables.
- A new CRM field is not usable until someone stages it. That is deliberate: someone has to decide
  what it means and write its definition.
- Nested arrays (the deal stage history) are parsed in the intermediate layer, not in staging.
