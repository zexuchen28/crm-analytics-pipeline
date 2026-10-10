"""Tests for the incremental extractor.

Run:  python -m unittest discover -s tests -t . -v      (or: pytest)
"""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone

from extract.client import CRMClient, ExtractionError
from extract.run import sync_all, sync_resource
from load.local_target import LocalWarehouse
from mock_crm_api.seed import build_dataset
from mock_crm_api.store import CRMStore, FailureInjector
from tests.support import start_server

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.injector = FailureInjector()
        self.store = CRMStore(build_dataset(seed=1, n_accounts=40, now=T0), injector=self.injector)
        self.server, base_url = start_server(self.store)
        self.sleeps: list[float] = []
        self.client = CRMClient(base_url, page_size=3, max_retries=3, backoff_base=0.01,
                                sleep=self.sleeps.append)  # record delays instead of waiting
        self.wh = LocalWarehouse(":memory:", engine="sqlite")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.wh.close()

    def assert_warehouse_matches_source(self, resource):
        source = {r["id"]: r for r in self.store.data[resource]}
        self.assertEqual(self.wh.fetch_payloads(resource), source)


class IncrementalSyncTests(PipelineTestCase):
    def test_first_run_loads_everything(self):
        result = sync_resource(self.client, self.wh, "deals")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.fetched, len(self.store.data["deals"]))
        self.assert_warehouse_matches_source("deals")
        self.assertEqual(self.wh.get_watermark("deals"),
                         max(d["updated_at"] for d in self.store.data["deals"]))

    def test_second_run_fetches_only_changes_and_creates_no_duplicates(self):
        sync_resource(self.client, self.wh, "deals", overlap_seconds=0)
        total = len(self.store.data["deals"])

        changes = self.store.mutate(n_updates=5, n_new=2, now=T0 + timedelta(days=1), seed=7)
        changed = len(changes["updated_ids"]) + len(changes["created_ids"])
        self.assertEqual(changed, 7)

        result = sync_resource(self.client, self.wh, "deals", overlap_seconds=0)
        # inclusive boundary: a few rows sharing the old watermark may come again
        self.assertGreaterEqual(result.fetched, changed)
        self.assertLess(result.fetched, total)
        self.assertEqual(self.wh.count("deals"), total + 2)       # new rows added, none duplicated
        self.assert_warehouse_matches_source("deals")             # updated rows were overwritten

    def test_rerun_without_changes_is_idempotent(self):
        sync_resource(self.client, self.wh, "deals")
        before = self.wh.fetch_payloads("deals")
        sync_resource(self.client, self.wh, "deals")
        sync_resource(self.client, self.wh, "deals")
        self.assertEqual(self.wh.fetch_payloads("deals"), before)
        self.assertEqual(self.wh.count("deals"), len(self.store.data["deals"]))

    def test_all_resources_sync(self):
        results = sync_all(self.client, self.wh)
        self.assertTrue(all(r.status == "ok" for r in results))
        for resource in ("accounts", "contacts", "deals", "campaigns", "usage"):
            self.assert_warehouse_matches_source(resource)

    def test_full_refresh_ignores_watermark(self):
        sync_resource(self.client, self.wh, "deals")
        result = sync_resource(self.client, self.wh, "deals", full_refresh=True)
        self.assertEqual(result.fetched, len(self.store.data["deals"]))


class FailureHandlingTests(PipelineTestCase):
    def test_transient_failure_is_retried_and_sync_succeeds(self):
        self.injector.configure(fail_on_page=2, fail_times=2, status=503)
        result = sync_resource(self.client, self.wh, "deals")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(self.sleeps), 2)                     # two failed attempts, two waits
        self.assert_warehouse_matches_source("deals")

    def test_rate_limit_honors_retry_after(self):
        self.injector.configure(fail_on_page=1, fail_times=2, status=429)
        sync_resource(self.client, self.wh, "deals")
        self.assertEqual(self.sleeps, [1.0, 1.0])                 # server said Retry-After: 1

    def test_failure_does_not_advance_watermark_or_load_partial_data(self):
        sync_resource(self.client, self.wh, "deals", overlap_seconds=0)
        watermark = self.wh.get_watermark("deals")
        snapshot = self.wh.fetch_payloads("deals")

        self.store.mutate(n_updates=5, n_new=2, now=T0 + timedelta(days=1), seed=7)
        self.injector.configure(fail_on_page=2, fail_times=999, status=503)   # hard outage on page 2

        with self.assertRaises(ExtractionError):
            sync_resource(self.client, self.wh, "deals", overlap_seconds=0)

        self.assertEqual(self.wh.get_watermark("deals"), watermark)           # did not move
        self.assertEqual(self.wh.fetch_payloads("deals"), snapshot)           # page 1 was NOT committed

        self.injector.configure()                                             # outage over
        result = sync_resource(self.client, self.wh, "deals", overlap_seconds=0)
        self.assertEqual(result.status, "ok")
        self.assertGreater(self.wh.get_watermark("deals"), watermark)
        self.assert_warehouse_matches_source("deals")                         # nothing was lost

    def test_one_failing_resource_does_not_block_the_others(self):
        self.injector.configure(fail_on_page=1, fail_times=999)
        results = {r.resource: r for r in sync_all(self.client, self.wh, resources=["deals"])}
        self.assertEqual(results["deals"].status, "failed")
        self.injector.configure()
        results = {r.resource: r for r in sync_all(self.client, self.wh, resources=["deals", "accounts"])}
        self.assertTrue(all(r.status == "ok" for r in results.values()))

    def test_client_errors_are_not_retried(self):
        with self.assertRaises(ExtractionError):
            self.client.get_json("/nope")                                     # 404
        self.assertEqual(self.sleeps, [])

    def test_bad_cursor_is_rejected(self):
        with self.assertRaises(ExtractionError):
            self.client.get_json("/deals", {"after": "not-a-cursor"})


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.wh = LocalWarehouse(":memory:", engine="sqlite")

    def test_duplicates_in_one_batch_keep_the_newest(self):
        self.wh.upsert("deals", [
            {"id": "d1", "updated_at": "2026-01-01T00:00:00Z", "stage": "Lead"},
            {"id": "d1", "updated_at": "2026-01-02T00:00:00Z", "stage": "Demo"},
        ])
        self.assertEqual(self.wh.count("deals"), 1)
        self.assertEqual(self.wh.fetch_payloads("deals")["d1"]["stage"], "Demo")

    def test_transaction_rolls_back_data_and_watermark_together(self):
        with self.assertRaises(RuntimeError):
            with self.wh.transaction():
                self.wh.upsert("deals", [{"id": "d1", "updated_at": "2026-01-01T00:00:00Z"}])
                self.wh.set_watermark("deals", "2026-01-01T00:00:00Z", 1)
                raise RuntimeError("boom")
        self.assertEqual(self.wh.count("deals"), 0)
        self.assertIsNone(self.wh.get_watermark("deals"))

    def test_record_without_updated_at_is_rejected(self):
        with self.assertRaises(ValueError):
            self.wh.upsert("deals", [{"id": "d1"}])

    def test_unknown_resource_name_is_rejected(self):
        with self.assertRaises(ValueError):
            self.wh.count("deals; DROP TABLE x")


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class DuckDBEngineTests(unittest.TestCase):
    """The same behavior on the DuckDB engine, which is the one used day to day."""

    def setUp(self):
        self.store = CRMStore(build_dataset(seed=1, n_accounts=20, now=T0), injector=FailureInjector())
        self.server, base_url = start_server(self.store)
        self.client = CRMClient(base_url, page_size=5, max_retries=1, backoff_base=0.01,
                                sleep=lambda seconds: None)
        self.wh = LocalWarehouse(":memory:", engine="duckdb")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.wh.close()

    def test_incremental_sync_updates_in_place(self):
        sync_resource(self.client, self.wh, "deals", overlap_seconds=0)
        self.store.mutate(n_updates=3, n_new=1, now=T0 + timedelta(days=1), seed=3)
        sync_resource(self.client, self.wh, "deals", overlap_seconds=0)
        source = {r["id"]: r for r in self.store.data["deals"]}
        self.assertEqual(self.wh.fetch_payloads("deals"), source)
        self.assertEqual(self.wh.count("deals"), len(source))

    def test_transaction_rolls_back_data_and_watermark_together(self):
        with self.assertRaises(RuntimeError):
            with self.wh.transaction():
                self.wh.upsert("deals", [{"id": "d1", "updated_at": "2026-01-01T00:00:00Z"}])
                self.wh.set_watermark("deals", "2026-01-01T00:00:00Z", 1)
                raise RuntimeError("boom")
        self.assertEqual(self.wh.count("deals"), 0)
        self.assertIsNone(self.wh.get_watermark("deals"))


if __name__ == "__main__":
    unittest.main()
