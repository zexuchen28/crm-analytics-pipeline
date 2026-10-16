"""Tests for commit() on both warehouse targets.

The BigQuery tests use a stand-in client, so they check the *order of operations* and the
SQL that is sent (load, then one transactional script, always clean up), not BigQuery itself.
Real BigQuery behavior is checked by running the extractor against a real project.
"""
import unittest

from load.bigquery_target import BigQueryWarehouse
from load.local_target import LocalWarehouse


class FakeBQ:
    """Just enough of google.cloud.bigquery for BigQueryWarehouse."""
    class SchemaField:
        def __init__(self, name, field_type, mode=None): self.name, self.field_type = name, field_type
    class LoadJobConfig:
        def __init__(self, schema=None, write_disposition=None): self.write_disposition = write_disposition
    class QueryJobConfig:
        def __init__(self, query_parameters=None): self.query_parameters = query_parameters or []
    class ScalarQueryParameter:
        def __init__(self, name, type_, value): self.name, self.type_, self.value = name, type_, value
    class WriteDisposition:
        WRITE_TRUNCATE = "WRITE_TRUNCATE"
    class Dataset:
        def __init__(self, ref): self.ref, self.location = ref, None


class FakeClient:
    def __init__(self, fail_load=False, fail_script=False, watermark_rows=None):
        self.calls = []                      # ordered log of ("kind", detail)
        self.loaded = {}                     # staging table -> rows
        self.fail_load, self.fail_script = fail_load, fail_script
        self.watermark_rows = watermark_rows or []

    def create_dataset(self, dataset, exists_ok=False):
        self.calls.append(("create_dataset", dataset.ref))

    def query(self, sql, job_config=None):
        is_script = sql.lstrip().startswith("BEGIN")
        self.calls.append(("script" if is_script else "query", (sql, job_config.query_parameters)))
        client = self
        class Job:
            def result(self_inner):
                if is_script and client.fail_script:
                    raise RuntimeError("script failed")
                return client.watermark_rows if "high_watermark FROM" in sql else []
        return Job()

    def load_table_from_json(self, rows, destination, job_config=None, location=None):
        self.calls.append(("load", destination))
        self.loaded[destination] = rows
        client = self
        class Job:
            def result(self_inner):
                if client.fail_load:
                    raise RuntimeError("load failed")
        return Job()

    def delete_table(self, table, not_found_ok=False):
        self.calls.append(("delete", table))

    def kinds(self):
        return [kind for kind, _ in self.calls]


def make_warehouse(**client_kwargs):
    client = FakeClient(**client_kwargs)
    wh = BigQueryWarehouse(project="proj", dataset="crm_raw", location="US", client=client, bq=FakeBQ)
    client.calls.clear()                     # forget the setup calls; tests look at commit() only
    return wh, client


RECORDS = [{"id": "d1", "updated_at": "2026-01-01T00:00:00Z", "stage": "Lead"},
           {"id": "d2", "updated_at": "2026-01-02T00:00:00Z", "stage": "Demo"}]


class BigQueryCommitTests(unittest.TestCase):
    def test_loads_staging_then_runs_one_transactional_script_then_cleans_up(self):
        wh, client = make_warehouse()
        wh.commit("deals", RECORDS, "2026-01-02T00:00:00Z")
        self.assertEqual(client.kinds(), ["load", "script", "delete"])
        script, params = [d for k, d in client.calls if k == "script"][0]
        self.assertIn("BEGIN TRANSACTION", script)
        self.assertIn("COMMIT TRANSACTION", script)
        self.assertIn("ROLLBACK TRANSACTION", script)                 # rolls back on any error
        self.assertLess(script.index("raw_deals"), script.index("_sync_state"))   # data first, watermark second
        self.assertEqual({p.name: p.value for p in params}["watermark"], "2026-01-02T00:00:00Z")

    def test_staging_table_is_dropped_even_when_the_script_fails(self):
        wh, client = make_warehouse(fail_script=True)
        with self.assertRaises(RuntimeError):
            wh.commit("deals", RECORDS, "2026-01-02T00:00:00Z")
        self.assertEqual(client.kinds(), ["load", "script", "delete"])

    def test_failed_load_never_touches_the_raw_table_or_the_watermark(self):
        wh, client = make_warehouse(fail_load=True)
        with self.assertRaises(RuntimeError):
            wh.commit("deals", RECORDS, "2026-01-02T00:00:00Z")
        self.assertNotIn("script", client.kinds())
        self.assertEqual(client.kinds()[-1], "delete")                # cleanup still attempted

    def test_duplicate_ids_in_a_batch_are_collapsed_before_loading(self):
        wh, client = make_warehouse()
        wh.commit("deals", RECORDS + [{"id": "d1", "updated_at": "2026-01-05T00:00:00Z", "stage": "Won"}],
                  "2026-01-05T00:00:00Z")
        (rows,) = client.loaded.values()
        self.assertEqual(sorted(r["id"] for r in rows), ["d1", "d2"])
        self.assertIn('"Won"', [r for r in rows if r["id"] == "d1"][0]["payload"])

    def test_no_records_only_updates_the_watermark_row(self):
        wh, client = make_warehouse()
        wh.commit("deals", [], "2026-01-02T00:00:00Z")
        self.assertEqual(client.kinds(), ["script"])
        script, _ = client.calls[0][1]
        self.assertIn("_sync_state", script)
        self.assertNotIn("raw_deals", script)

    def test_nothing_to_do_sends_nothing(self):
        wh, client = make_warehouse()
        wh.commit("deals", [], None)
        self.assertEqual(client.calls, [])

    def test_get_watermark(self):
        wh, _ = make_warehouse(watermark_rows=[{"high_watermark": "2026-01-02T00:00:00Z"}])
        self.assertEqual(wh.get_watermark("deals"), "2026-01-02T00:00:00Z")
        wh, _ = make_warehouse(watermark_rows=[])
        self.assertIsNone(wh.get_watermark("deals"))

    def test_unknown_resource_is_rejected(self):
        wh, _ = make_warehouse()
        with self.assertRaises(ValueError):
            wh.commit("deals; DROP TABLE x", RECORDS, None)

    def test_requires_a_project(self):
        import os
        old = os.environ.pop("GCP_PROJECT", None)
        try:
            with self.assertRaises(ValueError):
                BigQueryWarehouse(client=FakeClient(), bq=FakeBQ)
        finally:
            if old: os.environ["GCP_PROJECT"] = old


class LocalCommitTests(unittest.TestCase):
    def test_commit_saves_data_and_watermark_together(self):
        wh = LocalWarehouse(":memory:", engine="sqlite")
        wh.commit("deals", RECORDS, "2026-01-02T00:00:00Z")
        self.assertEqual(wh.count("deals"), 2)
        self.assertEqual(wh.get_watermark("deals"), "2026-01-02T00:00:00Z")

    def test_commit_with_a_bad_record_saves_nothing(self):
        wh = LocalWarehouse(":memory:", engine="sqlite")
        with self.assertRaises(ValueError):
            wh.commit("deals", RECORDS + [{"id": "d3"}], "2026-01-02T00:00:00Z")
        self.assertEqual(wh.count("deals"), 0)
        self.assertIsNone(wh.get_watermark("deals"))


if __name__ == "__main__":
    unittest.main()
