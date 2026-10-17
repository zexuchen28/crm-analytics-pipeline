"""Incremental sync: CRM API -> raw tables.

    python -m extract.run                       # all resources, incremental
    python -m extract.run --resources deals     # one resource
    python -m extract.run --full-refresh        # ignore stored watermarks

How a run stays safe to repeat
1. Read the stored watermark (max updated_at already loaded) for the resource.
2. Request only records changed since (watermark - overlap). The overlap window
   re-reads a few seconds/minutes of history to catch late writes.
3. Fetch ALL pages first. Nothing touches the warehouse until every page has
   arrived, so a failure on page N leaves the warehouse exactly as it was.
4. commit(): upsert by primary key and advance the watermark as ONE atomic step
   (a transaction locally, a staged MERGE script on BigQuery).
   Re-reading rows is harmless because the upsert is idempotent.

If any step fails the watermark does not move, and the next run covers the same
window again. (For very large resources, stage page by page and promote at the
end; the rule stays the same: the watermark moves last.)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass

from extract.client import CRMClient, ExtractionError
from extract.config import DEFAULT_BASE_URL, RESOURCES
from extract.timeutil import shift_ts
from load.local_target import LocalWarehouse

log = logging.getLogger("extract.run")


@dataclass
class SyncResult:
    resource: str
    status: str                      # "ok" or "failed"
    fetched: int = 0
    watermark_before: str | None = None
    watermark_after: str | None = None
    duration_s: float = 0.0
    error: str | None = None


def sync_resource(client: CRMClient, warehouse, resource: str,
                  overlap_seconds: int = 60, full_refresh: bool = False) -> SyncResult:
    started = time.monotonic()
    before = None if full_refresh else warehouse.get_watermark(resource)
    since = shift_ts(before, -overlap_seconds) if before else None
    log.info("%s: syncing since %s", resource, since or "the beginning")

    records: list[dict] = []
    for page in client.iter_pages(resource, updated_since=since):   # may raise ExtractionError
        records.extend(page)

    after = before
    if records:
        newest = max(r["updated_at"] for r in records)
        after = max(before, newest) if before else newest

    warehouse.commit(resource, records, after)   # data + watermark, all or nothing

    return SyncResult(resource, "ok", len(records), before, after, time.monotonic() - started)


def sync_all(client: CRMClient, warehouse, resources=RESOURCES,
             overlap_seconds: int = 60, full_refresh: bool = False) -> list[SyncResult]:
    """Sync each resource independently: one failing resource does not block the rest."""
    results = []
    for resource in resources:
        started = time.monotonic()
        try:
            results.append(sync_resource(client, warehouse, resource, overlap_seconds, full_refresh))
        except Exception as exc:   # expected failures log quietly, unexpected ones with a traceback
            if isinstance(exc, (ExtractionError, ValueError)):
                log.error("%s: FAILED, watermark left unchanged: %s", resource, exc)
            else:
                log.exception("%s: FAILED unexpectedly, watermark left unchanged", resource)
            results.append(SyncResult(resource, "failed", watermark_before=warehouse.get_watermark(resource),
                                      watermark_after=warehouse.get_watermark(resource),
                                      duration_s=time.monotonic() - started, error=str(exc)))
    return results


def print_summary(results: list[SyncResult]) -> None:
    print(f"\n{'resource':<10} {'status':<7} {'fetched':>7}  watermark")
    for r in results:
        moved = "unchanged" if r.watermark_before == r.watermark_after else f"{r.watermark_before} -> {r.watermark_after}"
        print(f"{r.resource:<10} {r.status:<7} {r.fetched:>7}  {moved}")
        if r.error:
            print(f"           error: {r.error}")


def build_warehouse(target: str, db: str | None = None, project: str | None = None,
                    dataset: str | None = None):
    """Create the warehouse for --target local|bigquery (shared with the quality checks)."""
    if target == "bigquery":
        # imported here so local runs never need the Google libraries installed
        from load.bigquery_target import BigQueryWarehouse
        warehouse = BigQueryWarehouse(project=project, dataset=dataset)
        log.info("warehouse: BigQuery %s.%s (%s)", warehouse.project, warehouse.dataset, warehouse.location)
        return warehouse
    warehouse = LocalWarehouse(db)
    log.info("warehouse: %s (%s)", warehouse.path, warehouse.engine)
    return warehouse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Incremental CRM -> warehouse sync")
    parser.add_argument("--target", choices=["local", "bigquery"], default="local",
                        help="where to load: a local DuckDB file, or BigQuery")
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT"),
                        help="GCP project id (bigquery target; or set GCP_PROJECT)")
    parser.add_argument("--dataset", default=os.getenv("BQ_RAW_DATASET", "crm_raw"),
                        help="BigQuery dataset for the raw tables (default crm_raw)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db", default=os.getenv("WAREHOUSE_PATH"), help="local warehouse file")
    parser.add_argument("--resources", nargs="+", choices=RESOURCES, default=list(RESOURCES))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--overlap-seconds", type=int, default=60)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--backoff-base", type=float, default=0.5)
    parser.add_argument("--full-refresh", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = CRMClient(args.base_url, page_size=args.page_size,
                       max_retries=args.max_retries, backoff_base=args.backoff_base)
    warehouse = build_warehouse(args.target, args.db, args.project, args.dataset)

    results = sync_all(client, warehouse, args.resources, args.overlap_seconds, args.full_refresh)
    print_summary(results)
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
