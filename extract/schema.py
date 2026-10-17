"""Schema-drift detection: compare what the CRM sends with a committed snapshot of what we expect.

Why: raw records are stored as JSON, so a CRM schema change never breaks the load. That is
also why it can go unnoticed: a renamed field just quietly stops feeding the models that read it.
Every sync checks the records it fetched against `schema_snapshot.json` and reports:

  new field     the CRM sends a field the snapshot does not know
  missing field a field the snapshot expects is absent from every fetched record
  type changed  a field now arrives as a different kind of value (string, number, ...)

Drift is reported, never silently absorbed; the load still goes ahead (raw JSON cannot break).
Accepting a change is a deliberate act: review it, then run `python -m extract.schema --update`
and commit the new snapshot. `python -m extract.run --fail-on-drift` turns drift into a failing exit code.

"Optional" fields may be absent. They are the fields the warehouse itself writes back to the CRM,
which exist only after the first reverse sync.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from extract.client import CRMClient
from extract.config import DEFAULT_BASE_URL, RESOURCES

SNAPSHOT_PATH = Path(__file__).with_name("schema_snapshot.json")

# Fields the warehouse writes into the CRM (see reverse_sync). Present only after a reverse sync.
WRITE_BACK_FIELDS = {"accounts": {"account_segment": "string", "usage_trend": "string",
                                  "needs_follow_up": "number"}}


def type_name(value) -> str | None:
    """JSON-level kind of a value. None means null, which says nothing about the type."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    return "object"


def observe(records: list[dict]) -> dict[str, set[str]]:
    """field -> set of non-null types seen across the records (empty set = only nulls)."""
    seen: dict[str, set[str]] = {}
    for record in records:
        for field, value in record.items():
            kinds = seen.setdefault(field, set())
            kind = type_name(value)
            if kind:
                kinds.add(kind)
    return seen


def infer(resource: str, records: list[dict]) -> dict:
    """Build a snapshot entry from records. Fields present in every record are required;
    fields present in only some, and the write-back fields, are optional."""
    seen = observe(records)
    in_all = {f for f in seen if all(f in r for r in records)}
    entry = {"required": {}, "optional": {}}
    for field, kinds in sorted(seen.items()):
        label = "|".join(sorted(kinds)) or "unknown"
        target = "required" if field in in_all and field not in WRITE_BACK_FIELDS.get(resource, {}) else "optional"
        entry[target][field] = label
    for field, label in WRITE_BACK_FIELDS.get(resource, {}).items():
        entry["required"].pop(field, None)
        entry["optional"].setdefault(field, label)
    return entry


def detect_drift(resource: str, records: list[dict], entry: dict | None) -> list[str]:
    """Differences between the records and the snapshot entry, as readable messages."""
    if not entry or not records:
        return []
    required, optional = entry.get("required", {}), entry.get("optional", {})
    observed = observe(records)
    messages = []
    new = sorted(set(observed) - set(required) - set(optional))
    if new:
        shown = ", ".join(f"{f} ({'|'.join(sorted(observed[f])) or 'null'})" for f in new)
        messages.append(f"new field(s) not in the schema snapshot: {shown}")
    missing = sorted(set(required) - set(observed))
    if missing:
        messages.append(f"field(s) missing from every record: {', '.join(missing)}")
    for field in sorted(observed):
        expected = required.get(field) or optional.get(field)
        if not expected or expected == "unknown" or not observed[field]:
            continue
        if not observed[field] <= set(expected.split("|")):
            messages.append(f"type changed: {field} expected {expected}, saw {'|'.join(sorted(observed[field]))}")
    return messages


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict:
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def save_snapshot(snapshot: dict, path: Path = SNAPSHOT_PATH) -> None:
    Path(path).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show or accept the CRM schema snapshot")
    parser.add_argument("--update", action="store_true",
                        help="rewrite the snapshot from the CRM as it is now (accepts any drift)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args(argv)

    client = CRMClient(args.base_url, page_size=200)
    snapshot = {}
    for resource in RESOURCES:
        records = [r for page in client.iter_pages(resource) for r in page]
        snapshot[resource] = infer(resource, records)
    if args.update:
        save_snapshot(snapshot)
        print(f"Wrote {SNAPSHOT_PATH}. Review the diff (git diff) and commit it if the change is intended.")
        return 0
    old = load_snapshot()
    drifted = False
    for resource in RESOURCES:
        problems = detect_drift(resource, [r for page in client.iter_pages(resource) for r in page], old.get(resource))
        for problem in problems:
            drifted = True
            print(f"{resource}: {problem}")
    print("Schema matches the snapshot." if not drifted else "\nDrift found. Review it; if intended, run with --update.")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
