"""Local warehouse used for development and tests (DuckDB; SQLite fallback).

Raw layout, one table per CRM object:

    raw_<resource>(id PRIMARY KEY, updated_at, payload, _ingested_at)

`payload` holds the full record as JSON. Keeping the raw layer schema-less means
a new or renamed CRM field can never break the load; dbt staging models parse
the JSON into typed columns, and schema-drift checks live in the quality layer.

Sync state is stored next to the data in `_sync_state`, one row per resource.
Data and watermark are written in ONE transaction, so they cannot disagree.
(The BigQuery target will reach the same guarantee by only advancing the
watermark after the load job succeeds.)

The SQL is limited to the subset DuckDB and SQLite share (`?` params,
INSERT ... ON CONFLICT DO UPDATE), so one code path serves both engines.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from extract.config import RESOURCES
from extract.timeutil import fmt_ts, utcnow
from load.common import prepare_rows, raw_table

log = logging.getLogger(__name__)


def detect_engine() -> str:
    try:
        import duckdb  # noqa: F401
        return "duckdb"
    except ImportError:
        log.warning("duckdb not installed; falling back to sqlite for the local warehouse")
        return "sqlite"


def default_path(engine: str) -> str:
    return "warehouse.duckdb" if engine == "duckdb" else "warehouse.sqlite"


class LocalWarehouse:
    def __init__(self, path: str | None = None, engine: str | None = None):
        self.engine = engine or detect_engine()
        self.path = path or default_path(self.engine)
        if self.engine == "duckdb":
            import duckdb
            self.con = duckdb.connect(self.path)
        else:
            # autocommit mode: transactions are opened explicitly in transaction()
            self.con = sqlite3.connect(self.path, isolation_level=None)
        self._create_tables()

    # -- schema ------------------------------------------------------------
    @staticmethod
    def table(resource: str) -> str:
        return raw_table(resource)  # whitelists the name, since it is interpolated into SQL

    def _create_tables(self) -> None:
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS _sync_state ("
            "resource VARCHAR PRIMARY KEY, high_watermark VARCHAR, "
            "last_success_at VARCHAR, rows_loaded BIGINT)")
        for resource in RESOURCES:
            self.con.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table(resource)} ("
                "id VARCHAR PRIMARY KEY, updated_at VARCHAR NOT NULL, "
                "payload VARCHAR NOT NULL, _ingested_at VARCHAR NOT NULL)")

    # -- transactions ------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.con.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")

    # -- state -------------------------------------------------------------
    def get_watermark(self, resource: str) -> str | None:
        row = self.con.execute(
            "SELECT high_watermark FROM _sync_state WHERE resource = ?", [resource]).fetchone()
        return row[0] if row else None

    def set_watermark(self, resource: str, watermark: str | None, rows_loaded: int) -> None:
        self.con.execute(
            "INSERT INTO _sync_state (resource, high_watermark, last_success_at, rows_loaded) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (resource) DO UPDATE SET high_watermark = excluded.high_watermark, "
            "last_success_at = excluded.last_success_at, rows_loaded = excluded.rows_loaded",
            [resource, watermark, fmt_ts(utcnow()), rows_loaded])

    # -- data --------------------------------------------------------------
    def commit(self, resource: str, records: list[dict], watermark: str | None) -> None:
        """Save a finished extract: the data AND the new watermark, in one transaction.
        Both are saved or neither is. This is the only call the extractor makes to write."""
        with self.transaction():
            self.upsert(resource, records)
            if watermark is not None:
                self.set_watermark(resource, watermark, len(records))

    def upsert(self, resource: str, records: list[dict]) -> int:
        """Insert new records and update existing ones, keyed on `id`.
        Safe to repeat: the same input always leaves the same table."""
        rows = prepare_rows(resource, records, fmt_ts(utcnow()))
        if not rows:
            return 0
        self.con.executemany(
            f"INSERT INTO {self.table(resource)} (id, updated_at, payload, _ingested_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET updated_at = excluded.updated_at, "
            "payload = excluded.payload, _ingested_at = excluded._ingested_at",
            [[r["id"], r["updated_at"], r["payload"], r["_ingested_at"]] for r in rows])
        return len(rows)

    def count(self, resource: str) -> int:
        return self.con.execute(f"SELECT COUNT(*) FROM {self.table(resource)}").fetchone()[0]

    def fetch_versions(self, resource: str) -> dict[str, str]:
        """id -> updated_at for every row; what reconciliation compares against the source."""
        rows = self.con.execute(f"SELECT id, updated_at FROM {self.table(resource)}").fetchall()
        return {rid: ts for rid, ts in rows}

    def fetch_payloads(self, resource: str) -> dict[str, dict]:
        rows = self.con.execute(f"SELECT id, payload FROM {self.table(resource)}").fetchall()
        return {rid: json.loads(payload) for rid, payload in rows}

    def close(self) -> None:
        self.con.close()
