"""Reconcile the warehouse against the source system, row by row.

    python -m quality.reconcile                    # local warehouse
    python -m quality.reconcile --target bigquery

An incremental sync only sees records that were created or changed. It can never see a
record that was DELETED at the source, and it cannot notice a sync that quietly fell behind.
This check re-reads the whole source and compares it with the warehouse, id by id:

  missing  in the CRM, not in the warehouse: the sync is behind (or a load lost rows)
  stale    in both, but the warehouse copy has a different updated_at
  extra    in the warehouse, gone from the CRM: deleted at the source

Exit code is 1 if anything differs, so a scheduled run or CI job fails loudly instead of
publishing numbers nobody can trust. Run it right after a sync: a CRM that changed in
between will legitimately show up as missing/stale.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from extract.client import CRMClient, ExtractionError
from extract.config import DEFAULT_BASE_URL, RESOURCES
from extract.run import build_warehouse
from extract.timeutil import fmt_ts, utcnow

log = logging.getLogger("quality.reconcile")
EXAMPLES = 3


@dataclass
class Report:
    resource: str
    source_count: int
    warehouse_count: int
    missing: list[str]
    stale: list[str]
    extra: list[str]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.stale or self.extra)


def scan_source(client: CRMClient, resource: str) -> dict[str, str]:
    """Read every record of a resource from the CRM; return id -> updated_at."""
    versions: dict[str, str] = {}
    for page in client.iter_pages(resource):
        for record in page:
            versions[record["id"]] = record["updated_at"]
    return versions


def compare(resource: str, source: dict[str, str], warehouse: dict[str, str]) -> Report:
    return Report(
        resource=resource,
        source_count=len(source),
        warehouse_count=len(warehouse),
        missing=sorted(set(source) - set(warehouse)),
        stale=sorted(i for i in set(source) & set(warehouse) if source[i] != warehouse[i]),
        extra=sorted(set(warehouse) - set(source)),
    )


def reconcile(client: CRMClient, warehouse, resources=RESOURCES) -> list[Report]:
    return [compare(r, scan_source(client, r), warehouse.fetch_versions(r)) for r in resources]


def write_report(reports: list[Report], path: Path) -> None:
    """Save the result as JSON so other tools (the dashboard) can show it."""
    Path(path).write_text(json.dumps({
        "generated_at": fmt_ts(utcnow()),
        "ok": all(r.ok for r in reports),
        "resources": [{"resource": r.resource, "source_count": r.source_count,
                       "warehouse_count": r.warehouse_count, "missing": len(r.missing),
                       "stale": len(r.stale), "extra": len(r.extra), "ok": r.ok} for r in reports],
    }, indent=2) + "\n")


def _examples(ids: list[str]) -> str:
    shown = ", ".join(ids[:EXAMPLES])
    return f"{len(ids)} (e.g. {shown})" if ids else "0"


def print_report(reports: list[Report]) -> None:
    print(f"\n{'resource':<10} {'source':>7} {'warehouse':>10}  result")
    for r in reports:
        print(f"{r.resource:<10} {r.source_count:>7} {r.warehouse_count:>10}  {'OK' if r.ok else 'MISMATCH'}")
        if r.missing:
            print(f"           missing in warehouse (sync is behind): {_examples(r.missing)}")
        if r.stale:
            print(f"           stale in warehouse (older copy):       {_examples(r.stale)}")
        if r.extra:
            print(f"           deleted at source, still in warehouse: {_examples(r.extra)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile the warehouse against the CRM")
    parser.add_argument("--target", choices=["local", "bigquery"], default="local")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db", default=os.getenv("WAREHOUSE_PATH"))
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT"))
    parser.add_argument("--dataset", default=os.getenv("BQ_RAW_DATASET", "crm_raw"))
    parser.add_argument("--resources", nargs="+", choices=RESOURCES, default=list(RESOURCES))
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--report", default="reconcile_report.json",
                        help="where to save the JSON result (use '' to skip)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = CRMClient(args.base_url, page_size=args.page_size)
    warehouse = build_warehouse(args.target, args.db, args.project, args.dataset)
    try:
        reports = reconcile(client, warehouse, args.resources)
    except ExtractionError as exc:
        print(f"Could not read the source system: {exc}")
        return 2
    print_report(reports)
    if args.report:
        write_report(reports, Path(args.report))
    bad = [r.resource for r in reports if not r.ok]
    if bad:
        print(f"\nRECONCILIATION FAILED for: {', '.join(bad)}. Do not publish numbers from these tables.")
        return 1
    print("\nAll resources reconcile: warehouse matches the source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
