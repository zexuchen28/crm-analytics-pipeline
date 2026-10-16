"""Pieces shared by every warehouse target, so local and BigQuery behave the same."""
from __future__ import annotations

import json

from extract.config import RESOURCES


def raw_table(resource: str) -> str:
    """Raw table name for a resource. Names are interpolated into SQL, so whitelist them."""
    if resource not in RESOURCES:
        raise ValueError(f"unknown resource: {resource}")
    return f"raw_{resource}"


def prepare_rows(resource: str, records: list[dict], ingested_at: str) -> list[dict]:
    """Validate records and turn them into raw-table rows, one per id (newest wins).

    A row is {id, updated_at, payload, _ingested_at}; `payload` is the whole record as JSON.
    """
    latest: dict[str, dict] = {}
    for rec in records:
        if "id" not in rec or "updated_at" not in rec:
            raise ValueError(f"{resource}: record without id/updated_at: {str(rec)[:120]}")
        current = latest.get(rec["id"])
        if current is None or rec["updated_at"] >= current["updated_at"]:
            latest[rec["id"]] = rec          # dedupe inside the batch
    return [
        {"id": r["id"], "updated_at": r["updated_at"],
         "payload": json.dumps(r, sort_keys=True), "_ingested_at": ingested_at}
        for r in latest.values()
    ]
