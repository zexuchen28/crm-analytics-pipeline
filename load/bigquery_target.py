"""BigQuery warehouse target.

Same contract as LocalWarehouse (get_watermark / commit / count), so the extractor
does not care which one it talks to.

How commit() stays all-or-nothing on BigQuery
  1. Load the extracted rows into a throwaway staging table with a load job (free, atomic).
  2. Run ONE script that, inside a transaction, MERGEs the staging table into the raw
     table and then MERGEs the new watermark into _sync_state.
     If anything fails the transaction is rolled back: data and watermark stay as they were.
  3. Always drop the staging table afterwards.
The watermark therefore moves in the same transaction as the data, never before it.

Needs billing enabled on the project: the free BigQuery sandbox has no DML (no MERGE).
Auth: `gcloud auth application-default login`. Config: GCP_PROJECT, BQ_RAW_DATASET, BQ_LOCATION.
"""
from __future__ import annotations

import logging
import os
import uuid

from extract.config import RESOURCES
from extract.timeutil import fmt_ts, utcnow
from load.common import prepare_rows, raw_table

log = logging.getLogger(__name__)

RAW_COLUMNS = [("id", "STRING"), ("updated_at", "STRING"), ("payload", "STRING"), ("_ingested_at", "STRING")]


class BigQueryWarehouse:
    def __init__(self, project: str | None = None, dataset: str | None = None,
                 location: str | None = None, client=None, bq=None):
        if bq is None:
            from google.cloud import bigquery as bq   # tests pass a stand-in instead
        self.bq = bq
        self.project = project or os.getenv("GCP_PROJECT")
        if not self.project:
            raise ValueError("No GCP project: pass --project or set GCP_PROJECT")
        self.dataset = dataset or os.getenv("BQ_RAW_DATASET", "crm_raw")
        self.location = location or os.getenv("BQ_LOCATION", "US")
        self.client = client or bq.Client(project=self.project, location=self.location)
        self._ensure_tables()

    # -- helpers -----------------------------------------------------------
    def _fq(self, table: str) -> str:
        return f"`{self.project}.{self.dataset}.{table}`"

    def _run(self, sql: str, params: list | None = None):
        config = self.bq.QueryJobConfig(query_parameters=params or [])
        return self.client.query(sql, job_config=config).result()

    def _ensure_tables(self) -> None:
        dataset = self.bq.Dataset(f"{self.project}.{self.dataset}")
        dataset.location = self.location
        self.client.create_dataset(dataset, exists_ok=True)
        for resource in RESOURCES:
            self._run(f"CREATE TABLE IF NOT EXISTS {self._fq(raw_table(resource))} "
                      "(id STRING NOT NULL, updated_at STRING NOT NULL, "
                      "payload STRING NOT NULL, _ingested_at STRING NOT NULL)")
        self._run(f"CREATE TABLE IF NOT EXISTS {self._fq('_sync_state')} "
                  "(resource STRING NOT NULL, high_watermark STRING, "
                  "last_success_at STRING, rows_loaded INT64)")

    # -- state -------------------------------------------------------------
    def get_watermark(self, resource: str) -> str | None:
        rows = list(self._run(
            f"SELECT high_watermark FROM {self._fq('_sync_state')} WHERE resource = @resource",
            [self.bq.ScalarQueryParameter("resource", "STRING", resource)]))
        return rows[0]["high_watermark"] if rows else None

    def count(self, resource: str) -> int:
        rows = list(self._run(f"SELECT COUNT(*) AS n FROM {self._fq(raw_table(resource))}"))
        return rows[0]["n"]

    def fetch_versions(self, resource: str) -> dict[str, str]:
        """id -> updated_at for every row; what reconciliation compares against the source."""
        rows = self._run(f"SELECT id, updated_at FROM {self._fq(raw_table(resource))}")
        return {row["id"]: row["updated_at"] for row in rows}

    # -- the one write call ------------------------------------------------
    def commit(self, resource: str, records: list[dict], watermark: str | None) -> None:
        table = raw_table(resource)                       # validates the resource name
        rows = prepare_rows(resource, records, fmt_ts(utcnow()))
        stage = f"_stg_{resource}_{uuid.uuid4().hex[:12]}" if rows else None
        try:
            if stage:
                self._load_stage(stage, rows)
            self._promote(table, stage, resource, watermark, len(rows))
        finally:
            if stage:                                     # runs on success AND failure
                self.client.delete_table(f"{self.project}.{self.dataset}.{stage}", not_found_ok=True)

    def _load_stage(self, stage: str, rows: list[dict]) -> None:
        schema = [self.bq.SchemaField(name, kind, mode="REQUIRED") for name, kind in RAW_COLUMNS]
        config = self.bq.LoadJobConfig(schema=schema, write_disposition=self.bq.WriteDisposition.WRITE_TRUNCATE)
        job = self.client.load_table_from_json(
            rows, f"{self.project}.{self.dataset}.{stage}", job_config=config, location=self.location)
        job.result()                                      # waits; raises if the load failed

    def _promote(self, table: str, stage: str | None, resource: str,
                 watermark: str | None, n_rows: int) -> None:
        statements, params = [], []
        if stage:
            statements.append(
                f"MERGE {self._fq(table)} AS t USING {self._fq(stage)} AS s ON t.id = s.id\n"
                "    WHEN MATCHED THEN UPDATE SET updated_at = s.updated_at, payload = s.payload, "
                "_ingested_at = s._ingested_at\n"
                "    WHEN NOT MATCHED THEN INSERT (id, updated_at, payload, _ingested_at) "
                "VALUES (s.id, s.updated_at, s.payload, s._ingested_at);")
        if watermark is not None:
            statements.append(
                f"MERGE {self._fq('_sync_state')} AS t\n"
                "    USING (SELECT @resource AS resource, @watermark AS high_watermark, "
                "@synced_at AS last_success_at, @n_rows AS rows_loaded) AS s\n"
                "    ON t.resource = s.resource\n"
                "    WHEN MATCHED THEN UPDATE SET high_watermark = s.high_watermark, "
                "last_success_at = s.last_success_at, rows_loaded = s.rows_loaded\n"
                "    WHEN NOT MATCHED THEN INSERT (resource, high_watermark, last_success_at, rows_loaded) "
                "VALUES (s.resource, s.high_watermark, s.last_success_at, s.rows_loaded);")
            P = self.bq.ScalarQueryParameter
            params = [P("resource", "STRING", resource), P("watermark", "STRING", watermark),
                      P("synced_at", "STRING", fmt_ts(utcnow())), P("n_rows", "INT64", n_rows)]
        if not statements:
            return
        script = ("BEGIN\n  BEGIN TRANSACTION;\n  " + "\n  ".join(statements) +
                  "\n  COMMIT TRANSACTION;\nEXCEPTION WHEN ERROR THEN\n  ROLLBACK TRANSACTION;\n  RAISE;\nEND;")
        self._run(script, params)
