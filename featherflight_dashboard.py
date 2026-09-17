#!/usr/bin/env python3
"""Local-only traffic dashboard for FeatherFlight mini booking app."""

import json
from pathlib import Path

import pandas as pd
import streamlit as st


TRAFFIC_LOG = Path(__file__).resolve().parent / ".runtime" / "traffic.jsonl"


def load_events():
    if not TRAFFIC_LOG.exists():
        return pd.DataFrame()

    rows = []
    for line in TRAFFIC_LOG.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("timestamp", ascending=False)
    return df


st.set_page_config(page_title="FeatherFlight Traffic")
st.title("FeatherFlight Traffic Dashboard")
st.caption("Local-only dashboard. Do not expose this port through Cloudflare Tunnel.")

df = load_events()
if df.empty:
    st.info("No booking attempts logged yet.")
    st.stop()

col1, col2, col3 = st.columns(3)
col1.metric("Attempts", len(df))
col2.metric("Unique emails", df["email"].nunique() if "email" in df else 0)
col3.metric("Successful attempts", int((df.get("status") == "success").sum()))

st.subheader("Usage by email")
email_counts = df.groupby("email", dropna=False).size().reset_index(name="attempts")
email_counts = email_counts.sort_values("attempts", ascending=False)
st.dataframe(email_counts, use_container_width=True)

st.subheader("Recent attempts")
display_cols = [
    col for col in [
        "timestamp",
        "email",
        "date",
        "time",
        "status",
        "family_count",
        "success_count",
        "failure_count",
        "appointment_id",
    ]
    if col in df.columns
]
st.dataframe(df[display_cols], use_container_width=True)

st.subheader("Status breakdown")
status_counts = df.groupby("status", dropna=False).size().reset_index(name="count")
st.dataframe(status_counts.sort_values("count", ascending=False), use_container_width=True)

if st.button("Refresh"):
    st.rerun()
