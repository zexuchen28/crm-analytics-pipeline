# 001: Incremental sync with a watermark, an overlap window, and upsert

## Context
The CRM changes constantly. Re-pulling everything each run is slow and does not scale; pulling only
"new" rows misses edits to existing deals (stage changes, amount changes).

## Decision
- Track a per-resource **high watermark**: the max `updated_at` already loaded.
- Request records with `updated_at >= watermark - overlap` (inclusive boundary, 60 s overlap by default).
- Load with an **upsert on the primary key**, so re-reading a row is harmless.
- Fetch **all pages before writing anything**, then write the data and advance the watermark in
  **one transaction**.
- Paginate with a **keyset cursor** on `(updated_at, id)`, not offsets, so edits made during a scan
  cannot shift rows past the reader.

## Why
- Edits are captured, not just inserts, because the filter is on `updated_at`.
- Late or tied timestamps are caught by the inclusive boundary and the overlap window.
- A failure at any point leaves data and watermark unchanged, so the next run covers the same window
  again. This is what makes a rerun always safe.

## Trade-offs
- Each run re-reads a small overlap (a few rows). Cheap, and it buys safety.
- Hard deletes in the CRM are not detected by this approach (they never show up as "updated").
  A periodic full-refresh reconciliation would be needed; planned for the data quality layer.
- Fetching all pages into memory first is fine at this size. For large resources: stage page by page
  and promote at the end, keeping the rule that the watermark moves last.
