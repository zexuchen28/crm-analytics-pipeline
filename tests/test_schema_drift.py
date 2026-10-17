"""Tests for schema-drift detection."""
import unittest
from datetime import datetime, timedelta, timezone

from extract.client import CRMClient
from extract.run import sync_all, sync_resource
from extract.schema import detect_drift, infer, load_snapshot, type_name
from load.local_target import LocalWarehouse
from mock_crm_api.seed import build_dataset
from mock_crm_api.store import CRMStore, FailureInjector
from tests.support import start_server

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.records = [{"id": "a", "n": 1, "s": "x", "maybe": None}, {"id": "b", "n": 2.5, "s": "y", "maybe": "z"}]
        self.entry = infer("deals", self.records)

    def test_a_matching_batch_has_no_drift(self):
        self.assertEqual(detect_drift("deals", self.records, self.entry), [])

    def test_a_new_field_is_reported_with_its_type(self):
        drifted = [dict(r, region="NA") for r in self.records]
        (message,) = detect_drift("deals", drifted, self.entry)
        self.assertIn("new field", message)
        self.assertIn("region (string)", message)

    def test_a_field_missing_from_every_record_is_reported(self):
        stripped = [{k: v for k, v in r.items() if k != "s"} for r in self.records]
        (message,) = detect_drift("deals", stripped, self.entry)
        self.assertIn("missing from every record: s", message)

    def test_a_changed_type_is_reported_but_int_vs_float_is_not(self):
        retyped = [dict(r, n="1") for r in self.records]
        (message,) = detect_drift("deals", retyped, self.entry)
        self.assertIn("type changed: n expected number, saw string", message)
        self.assertEqual(detect_drift("deals", [{"id": "a", "n": 7, "s": "x", "maybe": None}], self.entry), [])

    def test_nulls_say_nothing_about_type(self):
        self.assertEqual(detect_drift("deals", [{"id": "a", "n": None, "s": "x", "maybe": None}], self.entry), [])

    def test_no_baseline_or_no_records_means_no_opinion(self):
        self.assertEqual(detect_drift("deals", self.records, None), [])
        self.assertEqual(detect_drift("deals", [], self.entry), [])

    def test_type_names(self):
        self.assertEqual([type_name(v) for v in (None, True, 3, 2.5, "s", [1], {"a": 1})],
                         [None, "bool", "number", "number", "string", "list", "object"])

    def test_optional_fields_may_be_absent_and_write_back_fields_are_always_optional(self):
        entry = infer("accounts", [{"id": "a", "name": "x", "account_segment": "Small"}])
        self.assertIn("account_segment", entry["optional"])
        self.assertIn("usage_trend", entry["optional"])
        self.assertEqual(detect_drift("accounts", [{"id": "a", "name": "x"}], entry), [])


class CommittedSnapshotTests(unittest.TestCase):
    def test_the_committed_snapshot_matches_what_the_generator_produces(self):
        """If the mock CRM's fields change, this fails until the snapshot is updated on purpose."""
        snapshot = load_snapshot()
        data = build_dataset(seed=3, n_accounts=40, now=T0)
        store = CRMStore(data)
        store.mutate(n_updates=5, n_new=3, now=T0 + timedelta(days=1), seed=1)
        for resource, rows in store.data.items():
            with self.subTest(resource=resource):
                self.assertEqual(detect_drift(resource, rows, snapshot[resource]), [])


class SyncReportsDriftTests(unittest.TestCase):
    def setUp(self):
        self.store = CRMStore(build_dataset(seed=1, n_accounts=20, now=T0), injector=FailureInjector())
        self.server, url = start_server(self.store)
        self.client = CRMClient(url, page_size=50, max_retries=1, backoff_base=0.01, sleep=lambda s: None)
        self.wh = LocalWarehouse(":memory:", engine="sqlite")
        self.snapshot = load_snapshot()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_clean_sync_reports_no_drift(self):
        results = sync_all(self.client, self.wh, schema_snapshot=self.snapshot)
        self.assertEqual([r.drift for r in results], [[]] * 5)

    def test_a_schema_change_is_reported_and_the_data_still_lands(self):
        sync_all(self.client, self.wh, schema_snapshot=self.snapshot)
        self.store.alter_schema("accounts", "add", "region", now=T0 + timedelta(days=1))
        result = sync_resource(self.client, self.wh, "accounts", schema_snapshot=self.snapshot)
        self.assertEqual(result.status, "ok")                              # raw JSON cannot break
        self.assertIn("region", result.drift[0])
        self.assertEqual(self.wh.fetch_payloads("accounts")["acc_0001"]["region"], "NA")   # nothing lost

    def test_dropped_and_retyped_fields_are_reported(self):
        self.store.alter_schema("accounts", "drop", "owner", now=T0 + timedelta(days=1))
        self.store.alter_schema("accounts", "retype", "enrollment", now=T0 + timedelta(days=2))
        result = sync_resource(self.client, self.wh, "accounts", schema_snapshot=self.snapshot)
        text = " | ".join(result.drift)
        self.assertIn("missing from every record: owner", text)
        self.assertIn("type changed: enrollment expected number, saw string", text)

    def test_without_a_snapshot_nothing_is_reported(self):
        self.store.alter_schema("accounts", "add", "region", now=T0 + timedelta(days=1))
        self.assertEqual(sync_resource(self.client, self.wh, "accounts").drift, [])

    def test_bad_schema_actions_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.alter_schema("accounts", "explode", "x")
        with self.assertRaises(ValueError):
            self.store.alter_schema("nope", "add", "x")


if __name__ == "__main__":
    unittest.main()
