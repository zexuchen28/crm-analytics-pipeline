"""Tests for the reverse sync and the CRM write endpoint."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from extract.client import CRMClient
from mock_crm_api.seed import build_dataset
from mock_crm_api.store import CRMStore, FailureInjector
from reverse_sync.run import apply_plan, make_plan, read_crm, read_desired, run_reverse_sync
from tests.support import start_server

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


class StubMarts:
    """Stands in for the warehouse: returns what dim_accounts / account_health would."""
    def __init__(self, store, trend="declining"):
        self.store = store
        self.trend = trend

    def query_rows(self, sql):
        rows = []
        for i, acc in enumerate(self.store.data["accounts"]):
            customer = i % 3 == 0                              # a third of accounts have usage data
            rows.append({"account_id": acc["id"],
                         "account_segment": "Large" if acc["enrollment"] >= 15000 else "Small",
                         "usage_trend": self.trend if customer else None,
                         "needs_follow_up": (1 if self.trend == "declining" else 0) if customer else None})
        return rows


class ReverseSyncTestCase(unittest.TestCase):
    def setUp(self):
        self.injector = FailureInjector()
        self.store = CRMStore(build_dataset(seed=1, n_accounts=30, now=T0), injector=self.injector)
        self.server, url = start_server(self.store)
        self.client = CRMClient(url, page_size=200, max_retries=2, backoff_base=0.01, sleep=lambda s: None)
        self.marts = StubMarts(self.store)
        self.audit = Path(tempfile.mkdtemp()) / "audit.jsonl"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def crm(self):
        return {a["id"]: a for a in self.store.data["accounts"]}


class PlanTests(ReverseSyncTestCase):
    def test_null_values_are_never_written(self):
        desired = read_desired(self.marts)
        non_customers = [aid for aid, v in desired.items() if "usage_trend" not in v]
        self.assertTrue(non_customers)
        self.assertTrue(all(set(desired[a]) == {"account_segment"} for a in non_customers))

    def test_only_differences_are_planned(self):
        desired = read_desired(self.marts)
        crm = read_crm(self.client)
        first = sorted(desired)[0]
        crm[first].update(desired[first])                       # this account is already correct
        plan = make_plan(desired, crm)
        self.assertEqual(plan.unchanged, 1)
        self.assertEqual(len(plan.changes), len(desired) - 1)

    def test_accounts_deleted_in_the_crm_are_skipped_not_fatal(self):
        desired = read_desired(self.marts)
        crm = read_crm(self.client)
        gone = sorted(crm)[0]
        del crm[gone]
        plan = make_plan(desired, crm)
        self.assertEqual(plan.missing_in_crm, [gone])


class ApplyTests(ReverseSyncTestCase):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        before = json.dumps(self.store.data["accounts"], sort_keys=True)
        outcome = run_reverse_sync(self.client, self.marts, apply=False)
        self.assertFalse(outcome.applied)
        self.assertGreater(len(outcome.plan.changes), 0)
        self.assertEqual(json.dumps(self.store.data["accounts"], sort_keys=True), before)

    def test_apply_updates_the_crm_and_a_rerun_has_nothing_left_to_do(self):
        outcome = run_reverse_sync(self.client, self.marts, apply=True, audit_log=self.audit)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.updated, 30)
        desired = read_desired(self.marts)
        for aid, values in desired.items():
            for name, value in values.items():
                self.assertEqual(self.crm()[aid][name], value)
        again = run_reverse_sync(self.client, self.marts, apply=True, audit_log=self.audit)
        self.assertEqual(len(again.plan.changes), 0)             # idempotent
        self.assertEqual(again.updated, 0)

    def test_changed_value_bumps_updated_at_but_unchanged_does_not(self):
        run_reverse_sync(self.client, self.marts, apply=True)
        stamps = {a: r["updated_at"] for a, r in self.crm().items()}
        self.store.data["accounts"][0]["updated_at"] = "2026-01-01T00:00:00Z"
        stamps[self.store.data["accounts"][0]["id"]] = "2026-01-01T00:00:00Z"
        run_reverse_sync(self.client, self.marts, apply=True)    # nothing differs
        self.assertEqual({a: r["updated_at"] for a, r in self.crm().items()}, stamps)

    def test_a_new_trend_updates_only_the_accounts_that_have_one(self):
        run_reverse_sync(self.client, self.marts, apply=True)
        self.marts.trend = "stable"
        outcome = run_reverse_sync(self.client, self.marts, apply=True)
        self.assertEqual(outcome.updated, 10)                    # the 10 accounts with usage data
        self.assertTrue(all(len(c.new) == 2 for c in outcome.plan.changes))   # usage_trend + needs_follow_up

    def test_changes_are_sent_in_batches(self):
        plan = make_plan(read_desired(self.marts), read_crm(self.client))
        calls = []
        original = self.client.post_json
        self.client.post_json = lambda path, body: calls.append(len(body["inputs"])) or original(path, body)
        apply_plan(self.client, plan, batch_size=7, audit_log=None)
        self.assertEqual(calls, [7, 7, 7, 7, 2])

    def test_a_failed_batch_stops_the_run_and_a_rerun_finishes_the_job(self):
        self.injector.configure(fail_writes_after=1, fail_times=999, status=503)   # first batch ok, then outage
        outcome = run_reverse_sync(self.client, self.marts, apply=True, batch_size=10, audit_log=self.audit)
        self.assertFalse(outcome.ok)
        self.assertIn("re-run", outcome.failed)
        self.assertEqual(outcome.updated, 10)                    # the first batch landed
        self.injector.configure()                                # outage over
        again = run_reverse_sync(self.client, self.marts, apply=True, batch_size=10, audit_log=self.audit)
        self.assertTrue(again.ok)
        self.assertEqual(len(again.plan.changes), 20)            # only what was still missing
        self.assertEqual(again.updated, 20)
        for aid, values in read_desired(self.marts).items():
            for name, value in values.items():
                self.assertEqual(self.crm()[aid][name], value)

    def test_the_change_cap_refuses_an_unexpectedly_large_run(self):
        outcome = run_reverse_sync(self.client, self.marts, apply=True, max_changes=5)
        self.assertIn("max-changes", outcome.refused)
        self.assertEqual(outcome.updated, 0)
        self.assertFalse(any("account_segment" in a for a in self.store.data["accounts"]))

    def test_every_applied_change_lands_in_the_audit_log(self):
        outcome = run_reverse_sync(self.client, self.marts, apply=True, audit_log=self.audit)
        lines = [json.loads(line) for line in self.audit.read_text().splitlines()]
        expected = sum(len(c.new) for c in outcome.plan.changes)
        self.assertEqual(len(lines), expected)
        self.assertEqual(set(lines[0]), {"at", "account_id", "field", "old", "new"})
        self.assertIsNone(lines[0]["old"])                       # fields were unset before


class WriteEndpointTests(ReverseSyncTestCase):
    def test_non_writable_fields_are_rejected_per_record(self):
        aid = self.store.data["accounts"][0]["id"]
        result = self.store.batch_update("accounts", [{"id": aid, "properties": {"enrollment": 1}},
                                                      {"id": "acc_9999", "properties": {"usage_trend": "stable"}}])
        self.assertEqual(result.status, 200)
        errors = {e["id"]: e["error"] for e in result.body["errors"]}
        self.assertIn("not writable", errors[aid])
        self.assertEqual(errors["acc_9999"], "not_found")
        self.assertNotEqual(self.crm()[aid]["enrollment"], 1)

    def test_only_accounts_accept_writes_and_batches_are_capped(self):
        self.assertEqual(self.store.batch_update("deals", [{"id": "x", "properties": {}}]).status, 404)
        self.assertEqual(self.store.batch_update("accounts", []).status, 400)
        self.assertEqual(self.store.batch_update("accounts", [{"id": "a", "properties": {}}] * 101).status, 400)


if __name__ == "__main__":
    unittest.main()
