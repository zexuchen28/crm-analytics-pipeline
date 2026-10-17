"""CRM analytics dashboard.

    streamlit run dashboard/app.py

Reads the marts that dbt builds (it never computes a business metric itself) and shows the
three questions sales asked, plus the health of the pipeline behind the numbers.

Config (environment variables, all optional):
    DASHBOARD_TARGET   local (default) or bigquery
    WAREHOUSE_PATH     DuckDB file for the local target (default: warehouse.duckdb in the project root)
    GCP_PROJECT, BQ_RAW_DATASET   for the bigquery target
    DBT_RESULTS        path to dbt's run_results.json
    RECONCILE_REPORT   path to the JSON written by `python -m quality.reconcile`
"""
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# `streamlit run dashboard/app.py` puts only this file's folder on the import path, not the
# project root, so add the root before importing the project's own packages.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import queries  # noqa: E402
TARGET = os.getenv("DASHBOARD_TARGET", "local")
DB = os.getenv("WAREHOUSE_PATH", str(ROOT / "warehouse.duckdb"))
DBT_RESULTS = Path(os.getenv("DBT_RESULTS", ROOT / "dbt_project" / "target" / "run_results.json"))
RECONCILE = Path(os.getenv("RECONCILE_REPORT", "reconcile_report.json"))

st.set_page_config(page_title="CRM analytics", layout="wide")


def pct(value):
    return "n/a" if value is None else f"{value:.0%}"


def money(value):
    return "n/a" if value is None else f"${value:,.0f}"


@st.cache_data(ttl=60, show_spinner="Reading the warehouse...")
def load():
    warehouse = queries.open_warehouse(TARGET, db=DB, project=os.getenv("GCP_PROJECT"),
                                       dataset=os.getenv("BQ_RAW_DATASET", "crm_raw"))
    try:
        return queries.load_all(warehouse, DBT_RESULTS, RECONCILE)
    finally:
        close = getattr(warehouse, "close", None)
        if close:
            close()   # release the file so a sync or dbt build can write


st.title("CRM analytics")
st.caption("Synthetic EdTech CRM data. Numbers come from the dbt marts; nothing is calculated in this app.")

try:
    data = load()
except Exception as exc:  # most often: no warehouse file yet, or the marts are not built
    st.error("Could not read the marts. Run `make sync` and `make dbt-build` first, then refresh.")
    st.exception(exc)
    st.stop()

funnel_tab, channels_tab, usage_tab, health_tab = st.tabs(
    ["Sales funnel", "Marketing channels", "Customer usage", "Pipeline health"])

# ------------------------------------------------------------------ sales funnel
with funnel_tab:
    o = data["overview"]
    cols = st.columns(6)
    cols[0].metric("Deals", int(o["deals"]))
    cols[1].metric("Open", int(o["open_deals"] or 0))
    cols[2].metric("Won", int(o["won_deals"] or 0))
    cols[3].metric("Win rate", pct(o["win_rate"]))
    cols[4].metric("Won amount", money(o["won_amount"]))
    cols[5].metric("Avg days to close", "n/a" if o["avg_cycle_days"] is None else f"{o['avg_cycle_days']:.0f}")

    st.subheader("Where do deals get to?")
    st.caption("Deals that ever reached each stage, by buying motion. Deals lost along the way leave the funnel.")
    funnel = pd.DataFrame(data["funnel"])
    if funnel.empty:
        st.info("No deals yet.")
    else:
        funnel["step"] = funnel["stage_order"].astype(int).astype(str) + ". " + funnel["stage"]
        st.bar_chart(funnel.pivot(index="step", columns="buying_motion", values="deals"))
        shown = funnel[["buying_motion", "step", "deals", "conversion_from_previous"]].copy()
        shown["conversion_from_previous"] = shown["conversion_from_previous"].map(pct)
        st.dataframe(shown.set_index("buying_motion"))

    st.subheader("Where do deals wait longest?")
    durations = pd.DataFrame(data["durations"])
    if not durations.empty:
        durations["step"] = durations["stage_order"].astype(int).astype(str) + ". " + durations["stage"]
        st.bar_chart(durations.pivot(index="step", columns="buying_motion", values="avg_days_in_stage"))
        st.caption("Average days spent in a stage before moving on.")
        for _, row in durations[durations["dwell_rank"] == 1].iterrows():
            st.write(f"**{row['buying_motion']}**: slowest stage is {row['stage']} "
                     f"({row['avg_days_in_stage']:.1f} days on average).")

# ------------------------------------------------------------------ channels
with channels_tab:
    channels = pd.DataFrame(data["channels"])
    st.subheader("Which channels bring qualified deals?")
    st.caption("A deal is qualified once it reaches Demo or later. Each deal is credited to its source campaign.")
    if channels.empty:
        st.info("No channel data yet.")
    else:
        left, right = st.columns(2)
        with left:
            st.write("Qualified deals by channel")
            st.bar_chart(channels, x="channel", y="qualified_deals")
        with right:
            st.write("Cost per qualified deal (US$)")
            st.bar_chart(channels.dropna(subset=["cost_per_qualified_deal"]), x="channel", y="cost_per_qualified_deal")
        table = channels.copy()
        for column in ("qualification_rate", "win_rate"):
            table[column] = table[column].map(pct)
        st.dataframe(table.set_index("channel"))

# ------------------------------------------------------------------ customer usage
with usage_tab:
    health = pd.DataFrame(data["health"])
    st.subheader("Which customers are using the product less?")
    st.caption("Latest 4 weeks of average weekly active users compared with the 4 weeks before.")
    if health.empty:
        st.info("No usage data yet.")
    else:
        segments = st.multiselect("Account size", sorted(health["account_segment"].dropna().unique()),
                                  default=sorted(health["account_segment"].dropna().unique()))
        view = health[health["account_segment"].isin(segments)]
        cols = st.columns(4)
        cols[0].metric("Customers with usage data", len(view))
        cols[1].metric("Declining", int((view["usage_trend"] == "declining").sum()))
        cols[2].metric("Stable", int((view["usage_trend"] == "stable").sum()))
        cols[3].metric("Growing", int((view["usage_trend"] == "growing").sum()))

        st.write("Needs follow-up")
        follow_up = view[view["needs_follow_up"] == 1][
            ["account_name", "account_segment", "prior_avg_active_users", "recent_avg_active_users", "usage_change"]].copy()
        follow_up["usage_change"] = follow_up["usage_change"].map(pct)
        if follow_up.empty:
            st.success("No accounts need follow-up for the selected sizes.")
        else:
            st.dataframe(follow_up.set_index("account_name"))

        st.write("Weekly active users for one account")
        names = dict(zip(view["account_name"], view["account_id"]))
        if names:
            chosen = st.selectbox("Account", sorted(names))
            usage = pd.DataFrame(data["usage"])
            st.line_chart(usage[usage["account_id"] == names[chosen]], x="week_start", y="active_users")

# ------------------------------------------------------------------ pipeline health
with health_tab:
    st.subheader("Can you trust these numbers?")

    st.write("**Data freshness**")
    fresh = pd.DataFrame(data["freshness"])
    if fresh.empty:
        st.warning("Nothing has been synced yet.")
    else:
        cols = st.columns(len(fresh))
        for col, (_, row) in zip(cols, fresh.iterrows()):
            hours = row["hours_since_sync"]
            col.metric(row["resource"], row["status"], "n/a" if pd.isna(hours) else f"{hours:.0f}h ago", delta_color="off")
        st.dataframe(fresh.set_index("resource"))

    st.write("**Data checks (last dbt build)**")
    dbt = data["dbt"]
    if dbt is None:
        st.info("No dbt results found. Run `make dbt-build`.")
    else:
        cols = st.columns(4)
        cols[0].metric("Checks run", dbt["total"])
        cols[1].metric("Passed", dbt["passed"])
        cols[2].metric("Failed", dbt["failed"])
        cols[3].metric("Warnings", dbt["warned"])
        if dbt["failed"]:
            st.error("Failing checks: " + ", ".join(dbt["failed_ids"]))
        else:
            st.success(f"All checks passed (build finished {dbt['generated_at']}).")

    st.write("**Reconciliation against the CRM (last run)**")
    rec = data["reconcile"]
    if rec is None:
        st.info("No reconciliation report found. Run `make reconcile`.")
    else:
        if rec["ok"]:
            st.success(f"The warehouse matched the CRM row for row ({rec['generated_at']}).")
        else:
            st.error(f"The warehouse does NOT match the CRM ({rec['generated_at']}). Do not trust these numbers.")
        st.dataframe(pd.DataFrame(rec["resources"]).set_index("resource"))
