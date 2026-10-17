"""Tests for the dashboard's query layer (no browser, no Streamlit needed)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dashboard import queries
from load.local_target import LocalWarehouse


def marts_warehouse():
    """A tiny warehouse with an `analytics` schema shaped like the dbt marts."""
    wh = LocalWarehouse(":memory:", engine="sqlite")
    c = wh.con
    c.execute("attach ':memory:' as analytics")
    c.execute("create table analytics.fct_deals (deal_id, buying_motion, is_closed, is_won, is_lost, won_amount, cycle_days)")
    c.executemany("insert into analytics.fct_deals values (?,?,?,?,?,?,?)", [
        ("d1", "professor_led", 1, 1, 0, 1000.0, 10.0),
        ("d2", "professor_led", 1, 0, 1, 0.0, 20.0),
        ("d3", "professor_led", 0, 0, 0, 0.0, None),
        ("d4", "instructional_designer_led", 0, 0, 0, 0.0, None)])
    c.execute("create table analytics.int_deal_stage_history (deal_id, stage, stage_order)")
    c.executemany("insert into analytics.int_deal_stage_history values (?,?,?)", [
        ("d1", "Lead", 1), ("d1", "Discovery", 2), ("d1", "Closed Won", 6),
        ("d2", "Lead", 1), ("d2", "Closed Lost", 7),
        ("d3", "Lead", 1), ("d3", "Discovery", 2),
        ("d4", "Lead", 1)])
    c.execute("create table analytics.fct_stage_durations (stage, stage_order, buying_motion, completed_stage_visits, "
              "avg_days_in_stage, max_days_in_stage, dwell_rank)")
    c.execute("insert into analytics.fct_stage_durations values ('Lead', 1, 'professor_led', 3, 5.0, 9.0, 1)")
    c.execute("create table analytics.fct_channel_performance (channel, campaigns, deals_created, qualified_deals, won_deals, "
              "won_amount, spend, qualification_rate, win_rate, cost_per_qualified_deal, cost_per_won_deal)")
    c.execute("insert into analytics.fct_channel_performance values ('Email', 1, 4, 2, 1, 1000.0, 500.0, 0.5, 0.5, 250.0, 500.0)")
    c.execute("create table analytics.account_health (account_id, account_name, account_segment, enrollment, latest_active_users, "
              "recent_avg_active_users, prior_avg_active_users, usage_change, usage_trend, needs_follow_up, adoption_rate)")
    c.execute("insert into analytics.account_health values ('a1','Acme U','Large',20000,50,50.0,100.0,-0.5,'declining',1,0.01)")
    c.execute("create table analytics.stg_usage (account_id, week_start, active_users, sessions)")
    c.execute("insert into analytics.stg_usage values ('a1','2026-09-07',100,300)")
    wh.commit("accounts", [], "2026-10-10T08:00:00Z")      # writes a sync-state row only
    return wh


class OverviewAndFunnelTests(unittest.TestCase):
    def setUp(self):
        self.wh = marts_warehouse()

    def test_overview_counts_and_win_rate(self):
        o = queries.overview(self.wh)
        self.assertEqual((o["deals"], o["open_deals"], o["won_deals"], o["lost_deals"]), (4, 2, 1, 1))
        self.assertEqual(o["win_rate"], 0.5)                     # 1 won of 2 closed
        self.assertEqual(o["won_amount"], 1000.0)
        self.assertEqual(o["avg_cycle_days"], 15.0)

    def test_funnel_conversion_is_deals_over_the_previous_stage_and_excludes_closed_lost(self):
        rows = {(r["buying_motion"], r["stage"]): r for r in queries.funnel(self.wh)}
        self.assertNotIn(("professor_led", "Closed Lost"), rows)
        self.assertEqual(rows[("professor_led", "Lead")]["deals"], 3)
        self.assertIsNone(rows[("professor_led", "Lead")]["conversion_from_previous"])
        self.assertAlmostEqual(rows[("professor_led", "Discovery")]["conversion_from_previous"], 2 / 3)
        self.assertAlmostEqual(rows[("professor_led", "Closed Won")]["conversion_from_previous"], 1 / 2)
        self.assertEqual(rows[("instructional_designer_led", "Lead")]["deals"], 1)

    def test_win_rate_is_none_when_nothing_has_closed(self):
        self.wh.con.execute("update analytics.fct_deals set is_closed = 0, is_won = 0, is_lost = 0")
        self.assertIsNone(queries.overview(self.wh)["win_rate"])

    def test_decimals_become_floats(self):
        class Stub:
            def query_rows(self, sql):
                return [{"x": Decimal("1.50"), "y": "kept"}]
        self.assertEqual(queries._rows(Stub(), "select"), [{"x": 1.5, "y": "kept"}])
        self.assertIsInstance(queries._rows(Stub(), "select")[0]["x"], float)


class HealthTests(unittest.TestCase):
    NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

    def test_freshness_labels_by_age(self):
        rows = [{"resource": "a", "last_success_at": "2026-10-10T11:00:00Z"},
                {"resource": "b", "last_success_at": "2026-10-08T12:00:00Z"},
                {"resource": "c", "last_success_at": None}]
        status = {r["resource"]: (r["status"], r["hours_since_sync"]) for r in queries.freshness(rows, self.NOW)}
        self.assertEqual(status, {"a": ("fresh", 1.0), "b": ("stale", 48.0), "c": ("never synced", None)})

    def test_dbt_run_summary_counts_by_status(self):
        path = Path(tempfile.mkdtemp()) / "run_results.json"
        path.write_text(json.dumps({"metadata": {"generated_at": "2026-10-10T08:00:00Z"}, "results": [
            {"status": "pass", "unique_id": "t1"}, {"status": "success", "unique_id": "m1"},
            {"status": "fail", "unique_id": "test.bad"}, {"status": "warn", "unique_id": "t3"},
            {"status": "skipped", "unique_id": "t4"}]}))
        s = queries.dbt_run_summary(path)
        self.assertEqual((s["total"], s["passed"], s["failed"], s["warned"], s["skipped"]), (5, 2, 1, 1, 1))
        self.assertEqual(s["failed_ids"], ["test.bad"])

    def test_missing_files_mean_no_result_not_an_error(self):
        self.assertIsNone(queries.dbt_run_summary("/nonexistent/run_results.json"))
        self.assertIsNone(queries.reconcile_report("/nonexistent/report.json"))

    def test_reconcile_report_round_trips(self):
        from quality.reconcile import Report, write_report
        path = Path(tempfile.mkdtemp()) / "r.json"
        write_report([Report("deals", 10, 12, [], [], ["d1", "d2"]), Report("accounts", 5, 5, [], [], [])], path)
        rec = queries.reconcile_report(path)
        self.assertFalse(rec["ok"])
        self.assertEqual([(r["resource"], r["extra"], r["ok"]) for r in rec["resources"]],
                         [("deals", 2, False), ("accounts", 0, True)])


class LoadAllTests(unittest.TestCase):
    def test_load_all_returns_everything_the_app_needs(self):
        data = queries.load_all(marts_warehouse(), "/nonexistent/a.json", "/nonexistent/b.json")
        self.assertEqual(set(data), {"overview", "funnel", "durations", "channels", "health", "usage",
                                     "freshness", "dbt", "reconcile"})
        self.assertEqual(data["freshness"][0]["resource"], "accounts")
        self.assertEqual(data["health"][0]["usage_trend"], "declining")
        self.assertIsNone(data["dbt"])


class ReadOnlyTests(unittest.TestCase):
    def test_a_read_only_connection_can_read_and_cannot_write(self):
        path = str(Path(tempfile.mkdtemp()) / "w.sqlite")
        LocalWarehouse(path, engine="sqlite").commit("deals", [{"id": "d1", "updated_at": "2026-01-01T00:00:00Z"}], "2026-01-01T00:00:00Z")
        ro = LocalWarehouse(path, engine="sqlite", read_only=True)
        self.assertEqual(ro.count("deals"), 1)
        self.assertEqual(ro.sync_state()[0]["resource"], "deals")
        with self.assertRaises(Exception):
            ro.upsert("deals", [{"id": "d2", "updated_at": "2026-01-02T00:00:00Z"}])


if __name__ == "__main__":
    unittest.main()
