"""Reverse sync: write fields computed in the warehouse back to the CRM.

    python -m reverse_sync.run                 # DRY RUN: show what would change, write nothing
    python -m reverse_sync.run --apply         # send the changes
    python -m reverse_sync.run --target bigquery --apply

The marts compute things sales cares about (account size segment, whether usage is falling).
This puts them where sales works, as custom fields on the account:
    account_segment, usage_trend, needs_follow_up

How it stays safe
- Dry run by default. Nothing is written unless you pass --apply.
- It compares against the CRM as it is RIGHT NOW (a live read, not the warehouse copy) and
  sends only the accounts whose values differ. Running it twice sends nothing the second time.
- A cap (--max-changes) refuses to apply an unexpectedly large change set.
- Writes go in batches. If a batch fails, earlier batches stay applied and a rerun sends only
  what still differs; no cleanup needed.
- Every applied change is appended to an audit log (old value, new value, when).
- Missing values are never written: an account with no usage data keeps no usage_trend.
- The fields the warehouse writes are deliberately NOT read back by the dbt staging models,
  so the marts never depend on their own output.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from extract.client import CRMClient, ExtractionError
from extract.config import DEFAULT_BASE_URL
from extract.run import build_warehouse
from extract.schema import WRITE_BACK_FIELDS
from extract.timeutil import fmt_ts, utcnow

log = logging.getLogger("reverse_sync")

FIELDS = tuple(WRITE_BACK_FIELDS["accounts"])
SIGNALS_SQL = """
select a.account_id, a.account_segment, h.usage_trend, h.needs_follow_up
from analytics.dim_accounts a
left join analytics.account_health h on h.account_id = a.account_id
"""


@dataclass
class Change:
    account_id: str
    new: dict          # field -> value to write
    old: dict          # field -> value the CRM holds now (None if unset)


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    unchanged: int = 0
    missing_in_crm: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    plan: Plan
    applied: bool = False
    updated: int = 0
    errors: list[dict] = field(default_factory=list)
    failed: str | None = None          # set when a batch could not be sent
    refused: str | None = None         # set when the change cap stopped the run

    @property
    def ok(self) -> bool:
        return not (self.errors or self.failed or self.refused)


def read_desired(warehouse) -> dict[str, dict]:
    """What the marts say each account's fields should be. Null values are left out."""
    desired = {}
    for row in warehouse.query_rows(SIGNALS_SQL):
        values = {f: row[f] for f in FIELDS if row.get(f) is not None}
        if "needs_follow_up" in values:
            values["needs_follow_up"] = int(values["needs_follow_up"])
        desired[row["account_id"]] = values
    return desired


def read_crm(client: CRMClient) -> dict[str, dict]:
    """Every account as the CRM holds it right now."""
    return {rec["id"]: rec for page in client.iter_pages("accounts") for rec in page}


def make_plan(desired: dict[str, dict], crm: dict[str, dict]) -> Plan:
    plan = Plan()
    for account_id in sorted(desired):
        record = crm.get(account_id)
        if record is None:
            plan.missing_in_crm.append(account_id)       # deleted in the CRM: nothing to update
            continue
        new = {f: v for f, v in desired[account_id].items() if record.get(f) != v}
        if not new:
            plan.unchanged += 1
            continue
        plan.changes.append(Change(account_id, new, {f: record.get(f) for f in new}))
    return plan


def apply_plan(client: CRMClient, plan: Plan, batch_size: int, audit_log: Path | None) -> Outcome:
    outcome = Outcome(plan, applied=True)
    for start in range(0, len(plan.changes), batch_size):
        batch = plan.changes[start:start + batch_size]
        body = {"inputs": [{"id": c.account_id, "properties": c.new} for c in batch]}
        try:
            response = client.post_json("/accounts/batch_update", body)
        except ExtractionError as exc:
            outcome.failed = (f"batch starting at change {start + 1} of {len(plan.changes)} failed: {exc}. "
                              "Earlier batches were applied; re-run to send the rest.")
            return outcome
        done = set(response.get("updated", []))
        outcome.updated += len(done)
        outcome.errors.extend(response.get("errors", []))
        if audit_log:
            _audit(audit_log, [c for c in batch if c.account_id in done])
    return outcome


def _audit(path: Path, changes: list[Change]) -> None:
    now = fmt_ts(utcnow())
    with path.open("a") as fh:
        for change in changes:
            for name, new in change.new.items():
                fh.write(json.dumps({"at": now, "account_id": change.account_id, "field": name,
                                     "old": change.old.get(name), "new": new}) + "\n")


def run_reverse_sync(client: CRMClient, warehouse, apply: bool = False, batch_size: int = 50,
                     max_changes: int = 200, audit_log: Path | None = None) -> Outcome:
    plan = make_plan(read_desired(warehouse), read_crm(client))
    if not apply or not plan.changes:
        return Outcome(plan)
    if len(plan.changes) > max_changes:
        return Outcome(plan, refused=(f"{len(plan.changes)} accounts would change, more than --max-changes "
                                      f"({max_changes}). Check the marts, or raise the limit on purpose."))
    return apply_plan(client, plan, batch_size, audit_log)


def print_outcome(outcome: Outcome, apply: bool) -> None:
    plan = outcome.plan
    mode = "APPLY" if apply else "DRY RUN"
    print(f"\nReverse sync ({mode}): {len(plan.changes) + plan.unchanged + len(plan.missing_in_crm)} accounts checked")
    print(f"  to update: {len(plan.changes)}   already correct: {plan.unchanged}   "
          f"not in CRM anymore: {len(plan.missing_in_crm)}")
    if plan.changes:
        print(f"\n  {'account':<10} {'field':<17} current -> new")
        shown = 0
        for change in plan.changes:
            for name, new in change.new.items():
                if shown < 8:
                    print(f"  {change.account_id:<10} {name:<17} {change.old.get(name)!r} -> {new!r}")
                shown += 1
        if shown > 8:
            print(f"  ... and {shown - 8} more field changes")
    if outcome.refused:
        print(f"\nREFUSED: {outcome.refused}")
    elif outcome.failed:
        print(f"\nFAILED after updating {outcome.updated} accounts: {outcome.failed}")
    elif outcome.applied:
        print(f"\nUpdated {outcome.updated} accounts in the CRM.")
        for err in outcome.errors:
            print(f"  error on {err.get('id')}: {err.get('error')}")
    elif plan.changes:
        print("\nDry run only; nothing was written. Re-run with --apply to send these changes.")
    else:
        print("\nNothing to update: the CRM already matches the warehouse.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write warehouse-computed fields back to the CRM")
    parser.add_argument("--apply", action="store_true", help="send the changes (default is a dry run)")
    parser.add_argument("--target", choices=["local", "bigquery"], default="local")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db", default=os.getenv("WAREHOUSE_PATH"))
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT"))
    parser.add_argument("--dataset", default=os.getenv("BQ_RAW_DATASET", "crm_raw"))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-changes", type=int, default=200)
    parser.add_argument("--audit-log", default="reverse_sync_log.jsonl")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = CRMClient(args.base_url, page_size=200)
    warehouse = build_warehouse(args.target, args.db, args.project, args.dataset)
    try:
        outcome = run_reverse_sync(client, warehouse, args.apply, args.batch_size, args.max_changes,
                                   Path(args.audit_log))
    except ExtractionError as exc:
        print(f"Could not reach the CRM: {exc}")
        return 2
    except Exception as exc:
        if "dim_accounts" in str(exc) or "account_health" in str(exc):
            print("The marts are missing. Build them first: make dbt-build")
            return 2
        raise
    print_outcome(outcome, args.apply)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
