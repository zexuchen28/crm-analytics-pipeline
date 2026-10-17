"""Tests for the reconciliation check."""
import unittest
from datetime import datetime, timedelta, timezone

from extract.run import sync_all
from extract.client import CRMClient
from load.local_target import LocalWarehouse
from mock_crm_api.seed import build_dataset
from mock_crm_api.store import CRMStore, FailureInjector
from quality.reconcile import compare, print_report, reconcile
from tests.support import start_server

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


class CompareTests(unittest.TestCase):
    def test_identical_sides_are_ok(self):
        self.assertTrue(compare("deals", {"a": "t1", "b": "t2"}, {"a": "t1", "b": "t2"}).ok)

    def test_each_kind_of_difference_is_reported_separately(self):
        report = compare("deals", {"a": "t1", "b": "t2", "c": "t3"}, {"a": "t1", "b": "OLD", "z": "t9"})
        self.assertEqual(report.missing, ["c"])        # in the CRM, not loaded
        self.assertEqual(report.stale, ["b"])          # loaded, but an older version
        self.assertEqual(report.extra, ["z"])          # deleted in the CRM
        self.assertFalse(report.ok)


class ReconcileEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.store = CRMStore(build_dataset(seed=1, n_accounts=30, now=T0), injector=FailureInjector())
        self.server, url = start_server(self.store)
        self.client = CRMClient(url, page_size=25, max_retries=1, backoff_base=0.01, sleep=lambda s: None)
        self.wh = LocalWarehouse(":memory:", engine="sqlite")
        sync_all(self.client, self.wh, overlap_seconds=0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def failing(self, reports):
        return {r.resource: r for r in reports if not r.ok}

    def test_clean_after_a_sync(self):
        self.assertEqual(self.failing(reconcile(self.client, self.wh)), {})

    def test_detects_a_sync_that_is_behind_and_clears_after_syncing(self):
        changes = self.store.mutate(n_updates=4, n_new=2, now=T0 + timedelta(days=1), seed=3)
        bad = self.failing(reconcile(self.client, self.wh))
        self.assertEqual(set(bad), {"deals"})
        self.assertEqual(bad["deals"].missing, sorted(changes["created_ids"]))
        self.assertEqual(bad["deals"].stale, sorted(changes["updated_ids"]))
        sync_all(self.client, self.wh, overlap_seconds=0)
        self.assertEqual(self.failing(reconcile(self.client, self.wh)), {})

    def test_detects_hard_deletes_that_incremental_sync_cannot_see(self):
        deleted = self.store.delete_records("deals", n=3, seed=5)["deleted_ids"]
        sync_all(self.client, self.wh, overlap_seconds=0)             # the sync notices nothing...
        bad = self.failing(reconcile(self.client, self.wh))            # ...but reconciliation does
        self.assertEqual(bad["deals"].extra, deleted)
        self.assertEqual(bad["deals"].missing, [])

    def test_unknown_resource_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.store.delete_records("nope")

    def test_report_prints_examples_of_what_differs(self):
        self.store.delete_records("deals", n=2, seed=5)
        import io, contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_report(reconcile(self.client, self.wh))
        self.assertIn("MISMATCH", buffer.getvalue())
        self.assertIn("deleted at source", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
