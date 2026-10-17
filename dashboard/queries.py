"""Everything the dashboard shows, as plain functions returning lists of dicts.

All the logic lives here, not in the Streamlit file, so it can be tested without a browser:
- each function reads one mart (built by dbt) through `warehouse.query_rows`
- the funnel conversion math and the freshness rule are plain Python

The dashboard never computes a business metric itself; it only displays what the marts define.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

STALE_AFTER_HOURS = 26      # a daily sync is "stale" once it is more than a day and a bit old


def _rows(warehouse, sql: str) -> list[dict]:
    """Run a query; Decimals become floats so charts and tables can use them."""
    return [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}
            for row in warehouse.query_rows(sql)]


def open_warehouse(target: str = "local", db: str | None = None, project: str | None = None,
                   dataset: str | None = None):
    """A read-only connection, so the dashboard never blocks a sync or a dbt build on DuckDB."""
    if target == "bigquery":
        from load.bigquery_target import BigQueryWarehouse
        return BigQueryWarehouse(project=project, dataset=dataset)
    from load.local_target import LocalWarehouse
    return LocalWarehouse(db, read_only=True)


# ---------------------------------------------------------------- overview and funnel
def overview(warehouse) -> dict:
    (row,) = _rows(warehouse, """
        select count(*) as deals,
               sum(case when is_closed = 0 then 1 else 0 end) as open_deals,
               sum(is_won) as won_deals,
               sum(is_lost) as lost_deals,
               sum(won_amount) as won_amount,
               avg(cycle_days) as avg_cycle_days
        from analytics.fct_deals""")
    closed = (row["won_deals"] or 0) + (row["lost_deals"] or 0)
    row["win_rate"] = (row["won_deals"] or 0) / closed if closed else None
    return row


def funnel(warehouse) -> list[dict]:
    """Deals that ever reached each stage, per buying motion, with the conversion from the
    stage before. Closed Lost is not a funnel step; it is where deals leave the funnel."""
    rows = _rows(warehouse, """
        select h.stage, h.stage_order, d.buying_motion, count(distinct h.deal_id) as deals
        from analytics.int_deal_stage_history h
        join analytics.fct_deals d on d.deal_id = h.deal_id
        where h.stage <> 'Closed Lost'
        group by h.stage, h.stage_order, d.buying_motion""")
    result = []
    for motion in sorted({r["buying_motion"] for r in rows}):
        steps = sorted((r for r in rows if r["buying_motion"] == motion), key=lambda r: r["stage_order"])
        previous = None
        for step in steps:
            result.append({"buying_motion": motion, "stage": step["stage"], "stage_order": step["stage_order"],
                           "deals": step["deals"],
                           "conversion_from_previous": (step["deals"] / previous) if previous else None})
            previous = step["deals"]
    return result


def stage_durations(warehouse) -> list[dict]:
    return _rows(warehouse, """
        select stage, stage_order, buying_motion, completed_stage_visits,
               avg_days_in_stage, max_days_in_stage, dwell_rank
        from analytics.fct_stage_durations
        order by buying_motion, stage_order""")


# ---------------------------------------------------------------- channels
def channel_performance(warehouse) -> list[dict]:
    return _rows(warehouse, """
        select channel, campaigns, deals_created, qualified_deals, won_deals, won_amount, spend,
               qualification_rate, win_rate, cost_per_qualified_deal, cost_per_won_deal
        from analytics.fct_channel_performance
        order by qualified_deals desc""")


# ---------------------------------------------------------------- customer usage
def account_health(warehouse) -> list[dict]:
    return _rows(warehouse, """
        select account_id, account_name, account_segment, enrollment, latest_active_users,
               recent_avg_active_users, prior_avg_active_users, usage_change, usage_trend,
               needs_follow_up, adoption_rate
        from analytics.account_health
        order by needs_follow_up desc, usage_change""")


def usage_history(warehouse) -> list[dict]:
    return _rows(warehouse, """
        select account_id, week_start, active_users, sessions
        from analytics.stg_usage
        order by account_id, week_start""")


# ---------------------------------------------------------------- pipeline health
def freshness(sync_rows: list[dict], now: datetime | None = None,
              stale_after_hours: float = STALE_AFTER_HOURS) -> list[dict]:
    """How old is each resource's last successful sync, and is that too old?"""
    now = now or datetime.now(timezone.utc)
    result = []
    for row in sync_rows:
        last = row.get("last_success_at")
        hours = None
        if last:
            hours = (now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                     ).total_seconds() / 3600
        result.append({**row, "hours_since_sync": round(hours, 1) if hours is not None else None,
                       "status": "never synced" if hours is None else "stale" if hours > stale_after_hours else "fresh"})
    return result


def dbt_run_summary(path: str | Path) -> dict | None:
    """Result of the last `dbt build`, from dbt's run_results.json. None if dbt has not run."""
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    results = data.get("results", [])
    status = lambda r: r.get("status")
    failed = [r.get("unique_id") for r in results if status(r) in ("fail", "error")]
    return {"generated_at": data.get("metadata", {}).get("generated_at"),
            "total": len(results),
            "passed": sum(status(r) in ("pass", "success") for r in results),
            "warned": sum(status(r) == "warn" for r in results),
            "skipped": sum(status(r) == "skipped" for r in results),
            "failed": len(failed), "failed_ids": failed}


def reconcile_report(path: str | Path) -> dict | None:
    """Result of the last reconciliation, from the JSON `quality.reconcile` writes. None if never run."""
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


def load_all(warehouse, dbt_results: str | Path, reconcile_path: str | Path) -> dict:
    """Everything the dashboard needs, read in one go (so the connection can be closed)."""
    return {
        "overview": overview(warehouse),
        "funnel": funnel(warehouse),
        "durations": stage_durations(warehouse),
        "channels": channel_performance(warehouse),
        "health": account_health(warehouse),
        "usage": usage_history(warehouse),
        "freshness": freshness(warehouse.sync_state()),
        "dbt": dbt_run_summary(dbt_results),
        "reconcile": reconcile_report(reconcile_path),
    }
