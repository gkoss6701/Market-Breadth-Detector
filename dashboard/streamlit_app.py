"""
Streamlit dashboard: pick any registered index (major or sector) from a
selector, see its composite score/regime and full metric history. Also
shows a cross-index summary table so you can see which sectors are
currently strongest/weakest without switching the selector repeatedly.

Reads from the same SQLite DB the GitHub Actions workflows write to.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import pandas as pd
import streamlit as st

from src.db.models import (
    get_breadth_history,
    get_connection,
    get_index_registry,
    get_latest_snapshot_all_indexes,
)

st.set_page_config(page_title="Market Breadth Detector", layout="wide")
st.title("Market Breadth Detector")

registry = get_index_registry()

if registry.empty:
    st.info("No indexes registered yet -- run scripts/refresh_universe.py first, "
            "then daily_ingest.py / backfill_history.py, then breadth_compute.py.")
    st.stop()

# ---------------- Index selector ----------------
major = registry[registry["index_type"] == "major"]
sector = registry[registry["index_type"] == "sector"]

label_to_key = dict(zip(registry["label"], registry["index_key"]))
ordered_labels = list(major["label"]) + list(sector["label"])

selected_label = st.selectbox("Index", ordered_labels, index=0)
selected_key = label_to_key[selected_label]

breadth = get_breadth_history(selected_key)

if breadth.empty:
    st.info(f"No breadth data yet for {selected_label} -- run breadth_compute.py after "
            "prices have been ingested for this index's tickers.")
    st.stop()

# ---------------- Top tiles ----------------
latest = breadth.iloc[-1]
col1, col2, col3, col4 = st.columns(4)
col1.metric("Composite Score", f"{latest['composite_score']:.2f}" if pd.notna(latest["composite_score"]) else "n/a")
col2.metric("Regime", latest["regime"] if pd.notna(latest["regime"]) else "unknown")
col3.metric("% Above 50MA", f"{latest['pct_above_50ma']:.1f}%" if pd.notna(latest["pct_above_50ma"]) else "n/a")
nh = int(latest["new_highs"]) if pd.notna(latest["new_highs"]) else 0
nl = int(latest["new_lows"]) if pd.notna(latest["new_lows"]) else 0
col4.metric("New Highs / New Lows", f"{nh} / {nl}")

# ---------------- Charts ----------------
st.subheader("Composite Score Over Time")
st.line_chart(breadth.set_index("date")["composite_score"])

st.subheader("% of Stocks Above Moving Average")
pct_ma = breadth.set_index("date")[["pct_above_50ma", "pct_above_200ma"]].rename(
    columns={"pct_above_50ma": "% Above 50-day MA", "pct_above_200ma": "% Above 200-day MA"}
)
st.line_chart(pct_ma)

st.subheader("Advance-Decline Line")
st.caption("Cumulative advancers minus decliners within this index's universe.")
ad_line = breadth.set_index("date")[["ad_line"]].rename(columns={"ad_line": "A/D Line"})
st.line_chart(ad_line)

st.subheader("New 52-Week Highs vs. Lows")
nh_nl = breadth.set_index("date")[["new_highs", "new_lows"]].rename(
    columns={"new_highs": "New Highs", "new_lows": "New Lows"}
)
st.line_chart(nh_nl)

# ---------------- Cross-index summary ----------------
st.divider()
st.subheader("All Indexes -- Latest Snapshot")
st.caption("Quick scan across every tracked index/sector without switching the selector above.")

snapshot = get_latest_snapshot_all_indexes()
if not snapshot.empty:
    merged = snapshot.merge(registry, on="index_key", how="left")
    merged = merged.sort_values(["index_type", "composite_score"], ascending=[True, False])
    display_cols = ["label", "index_type", "date", "composite_score", "regime",
                     "pct_above_50ma", "new_highs", "new_lows"]
    display = merged[display_cols].rename(columns={
        "label": "Index", "index_type": "Type", "date": "Date",
        "composite_score": "Composite Score", "regime": "Regime",
        "pct_above_50ma": "% Above 50MA", "new_highs": "New Highs", "new_lows": "New Lows",
    })
    st.dataframe(display, use_container_width=True, hide_index=True)
else:
    st.caption("No cross-index data yet.")

# ---------------- Alerts ----------------
st.divider()
st.subheader("Recent Alerts")
with get_connection() as conn:
    alerts = pd.read_sql(
        "SELECT a.*, m.label FROM alerts_sent a LEFT JOIN index_metadata m "
        "ON a.index_key = m.index_key ORDER BY a.sent_at DESC LIMIT 20",
        conn,
    )
st.dataframe(alerts, use_container_width=True)
