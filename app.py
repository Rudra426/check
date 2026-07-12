"""
Streamlit dashboard for the customer-segmentation pipeline — UI unchanged,
now calls a FastAPI backend instead of running the pipeline in-process.
Run: streamlit run app.py
Backend: uvicorn main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import has_api_key  # noqa: E402

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Customer Segmentation", layout="wide")


def init_state() -> None:
    defaults = {
        "session_id": None,
        "uploaded_name": None,
        "raw_preview": None,
        "raw_shape": None,
        "mapping_report": None,
        "editable_mapping": None,
        "field_choices": None,
        "mapping_confirmed": False,
        "report": None,
        "cluster_out": None,
        "labeled": None,
        "chat_history": [],
    }
    for key, val in defaults.items():
        st.session_state.setdefault(key, val)


def reset_downstream() -> None:
    for key in (
        "mapping_report", "editable_mapping", "field_choices", "mapping_confirmed",
        "report", "cluster_out", "labeled", "chat_history",
    ):
        st.session_state[key] = [] if key == "chat_history" else None
    st.session_state.mapping_confirmed = False


def api_post(path: str, **kwargs):
    resp = requests.post(f"{API_URL}{path}", **kwargs)
    if not resp.ok:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        st.error(f"Backend error: {detail}")
        st.stop()
    return resp.json()


def api_get(path: str, **kwargs):
    resp = requests.get(f"{API_URL}{path}", **kwargs)
    if not resp.ok:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        st.error(f"Backend error: {detail}")
        st.stop()
    return resp.json()


def render_schema_confirm() -> None:
    st.subheader("2 — Confirm column mapping")
    report = st.session_state.mapping_report
    status = report["status"]

    if status == "rejected":
        st.error("This file does not look like e-commerce order data.")
        for msg in report["messages"]:
            st.write(f"- {msg}")
        for tip in report["suggestions"]:
            st.info(tip)
        st.stop()

    if status == "needs_confirmation":
        st.warning("Some columns need your confirmation before continuing.")
        for msg in report["messages"]:
            st.write(f"- {msg}")
    else:
        st.success("All required fields mapped with high confidence.")

    choices = st.session_state.field_choices
    proposals = st.session_state.editable_mapping
    overrides: dict[str, str] = {}

    st.write("Map each uploaded column to an internal field (or 'none' to ignore):")
    for raw, proposed in proposals.items():
        c1, c2 = st.columns([3, 2])
        with c1:
            picked = st.selectbox(
                raw,
                options=choices,
                index=choices.index(proposed) if proposed in choices else len(choices) - 1,
                key=f"map_{raw}",
            )
        with c2:
            st.caption(f"LLM proposal: {proposed}")
        overrides[raw] = picked

    if st.button("Confirm mapping and continue", type="primary"):
        result = api_post(f"/api/finalize_mapping/{st.session_state.session_id}", json=overrides)
        if result["status"] == "rejected":
            st.error("With these choices, required fields are still missing.")
            for msg in result["report"]["messages"]:
                st.write(f"- {msg}")
        else:
            st.session_state.mapping_confirmed = True
            st.session_state.report = None
            st.session_state.cluster_out = None
            st.session_state.labeled = None
            st.success(
                f"Mapping confirmed — {result['shape'][1]} fields, {result['shape'][0]} rows."
            )
            st.rerun()


def render_validation() -> None:
    st.subheader("3 — Data validation")
    if st.session_state.report is None:
        with st.spinner("Cleaning and validating..."):
            data = api_post(f"/api/clean/{st.session_state.session_id}")
        st.session_state.report = data["report"]

    report = st.session_state.report

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows in", report["rows_in"])
    c2.metric("Rows kept", report["rows_out"], delta=-report["rows_dropped_total"])
    c3.metric("Customers", report["n_customers"])
    c4.metric("Orders", report["n_orders"])

    for w in report.get("warnings", []):
        st.warning(w)

    with st.expander("Cleaning details"):
        drops = report["drops"]
        st.write(
            f"- Null required fields removed: {drops['null_required']['rows_dropped']} "
            f"({drops['null_required']['per_field']})"
        )
        st.write(f"- Duplicate orders removed: {drops['duplicate_orders']['duplicates_removed']}")
        st.write(
            f"- Negative/zero values removed: "
            f"neg={drops['nonpositive_values']['negative_removed']}, "
            f"zero={drops['nonpositive_values']['zero_removed']}"
        )
        st.write(f"- Future-dated orders removed: {drops['future_orders']}")

        id_info = report.get("id_normalization", {})
        if id_info.get("merged_groups"):
            st.write(
                f"- Customer ids unified: {id_info['ids_before']} raw ids "
                f"→ {id_info['ids_after']} customers "
                f"({id_info['merged_groups']} groups merged)"
            )
            for ex in id_info.get("examples", []):
                st.write(f"   - {ex['variants']} → {ex['merged_to']}")

        order_info = report.get("order_normalization", {})
        if order_info.get("duplicate_placeholders"):
            st.write(
                f"- {order_info['duplicate_placeholders']} orders had a literal "
                f"'DUPLICATE' placeholder instead of an id (treated as missing)."
            )

        if report.get("date_range"):
            dr = report["date_range"]
            st.write(f"- Date range: {dr['min']} to {dr['max']} (as of {dr['reference']})")

        if report.get("optional_field_nulls"):
            st.write(f"- Optional-field nulls: {report['optional_field_nulls']}")

    if report["rows_out"] == 0:
        st.error("No rows survived cleaning — cannot segment. Check the source data.")
        st.stop()


def render_clustering() -> None:
    st.subheader("4 — Customer segments")

    if st.session_state.cluster_out is None:
        if not st.button("Segment customers", type="primary"):
            st.info("Click to engineer features and cluster customers.")
            return
        with st.spinner("Engineering features and clustering..."):
            data = api_post(f"/api/cluster/{st.session_state.session_id}")
        st.session_state.cluster_out = data
        st.session_state.labeled = None

    out = st.session_state.cluster_out
    metrics = out["metrics"]
    result = pd.DataFrame(out["chart_data"])

    c1, c2, c3 = st.columns(3)
    c1.metric("Segments (k)", metrics["k"])
    c2.metric("Silhouette", metrics["silhouette"], help="Higher is better (-1 to 1)")
    c3.metric("Davies-Bouldin", metrics["davies_bouldin"], help="Lower is better")

    plot_df = result.copy()
    plot_df["cluster"] = plot_df["cluster"].astype(str)

    sizes = plot_df["cluster"].value_counts().sort_index().reset_index()
    sizes.columns = ["cluster", "customers"]
    st.plotly_chart(
        px.bar(sizes, x="cluster", y="customers", color="cluster", title="Segment sizes"),
        use_container_width=True,
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if "recency" in plot_df.columns and "monetary" in plot_df.columns:
            st.plotly_chart(
                px.scatter(
                    plot_df, x="recency", y="monetary", color="cluster",
                    hover_name="customer_id", title="Recency vs Monetary",
                ),
                use_container_width=True,
            )
    with col_b:
        if "umap_x" in plot_df.columns and "umap_y" in plot_df.columns:
            st.plotly_chart(
                px.scatter(
                    plot_df, x="umap_x", y="umap_y", color="cluster",
                    hover_name="customer_id", title="UMAP projection",
                ),
                use_container_width=True,
            )


def render_actions() -> None:
    st.subheader("5 — Recommended actions per segment")

    if st.session_state.labeled is None:
        with st.spinner("Naming segments and assigning actions with the LLM..."):
            data = api_post(f"/api/label/{st.session_state.session_id}")
        st.session_state.labeled = data["segments"]

    segments = st.session_state.labeled
    for seg in segments:
        with st.container(border=True):
            top = st.columns([3, 1, 1])
            top[0].markdown(f"**{seg['persona']}**")
            top[1].metric("Customers", seg["size"])
            top[2].metric("Share", f"{seg['share_pct']}%")

            st.markdown(
                f"**Action:** {seg['action']} · **Channel:** {seg['channel']} · "
                f"**Priority:** {seg['priority']} (score {seg['priority_score']})"
            )
            avg = seg["averages"]
            st.caption(
                f"Avg — recency: {avg.get('recency', '-')}d · "
                f"frequency: {avg.get('frequency', '-')} · "
                f"monetary: {avg.get('monetary', '-')} · "
                f"AOV: {avg.get('aov', '-')}"
            )
            if seg.get("reasoning"):
                st.caption(seg["reasoning"])


def render_revenue_impact() -> None:
    st.subheader("Revenue Impact")
    data = api_get(f"/api/revenue/{st.session_state.session_id}")
    conc = data["concentration"]
    atrisk = data["at_risk"]

    if not conc:
        st.info("No positive revenue values available to analyze.")
        return

    top = conc[0]
    st.markdown(
        f"**{top['segment']}** = {top['pct_of_customers']}% of customers but "
        f"{top['pct_of_revenue']}% of revenue"
    )

    total_revenue = data["total_revenue"]
    total_customers = data["total_customers"]
    avg_rev = total_revenue / total_customers if total_customers else 0.0

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total revenue", f"${total_revenue:,.2f}")
    k2.metric(
        "CLV at risk", f"${atrisk['total_clv_at_risk']:,.2f}",
        delta=f"{atrisk['pct_of_total_clv_at_risk']}% of CLV", delta_color="inverse",
    )
    k3.metric(
        "Customers at risk", f"{atrisk['customer_count_at_risk']:,}",
        delta=f"{atrisk['pct_of_total_customers_at_risk']}% of base", delta_color="inverse",
    )
    k4.metric("Avg revenue/customer", f"${avg_rev:,.2f}")

    if atrisk["any_at_risk"]:
        st.error(
            f"${atrisk['total_clv_at_risk']:,.2f} "
            f"({atrisk['pct_of_total_clv_at_risk']}% of total CLV) sits in at-risk "
            f"segments across {atrisk['customer_count_at_risk']} customers: "
            f"{', '.join(atrisk['matched_segments'])}."
        )
    else:
        st.info("No at-risk segment found among the current personas.")

    long_df = pd.DataFrame(conc).melt(
        id_vars="segment", value_vars=["pct_of_customers", "pct_of_revenue"],
        var_name="metric", value_name="pct",
    )
    long_df["metric"] = long_df["metric"].map(
        {"pct_of_customers": "% of customers", "pct_of_revenue": "% of revenue"}
    )
    st.plotly_chart(
        px.bar(
            long_df, x="segment", y="pct", color="metric", barmode="group",
            title="Customer share vs revenue share by segment",
            labels={"pct": "Percent", "segment": "Segment"},
        ),
        use_container_width=True,
    )

    display = pd.DataFrame({
        "Segment": [r["segment"] for r in conc],
        "Customers": [f"{r['customer_count']:,}" for r in conc],
        "% of customers": [f"{r['pct_of_customers']}%" for r in conc],
        "Total revenue": [f"${r['total_revenue']:,.2f}" for r in conc],
        "% of revenue": [f"{r['pct_of_revenue']}%" for r in conc],
        "Avg revenue/customer": [f"${r['avg_revenue_per_customer']:,.2f}" for r in conc],
    })
    st.dataframe(display, use_container_width=True, hide_index=True)

    if data.get("excluded_count"):
        st.caption(f"{data['excluded_count']} customers excluded from revenue math (missing/zero spend).")


def render_download() -> None:
    st.subheader("6 — Export")
    resp = requests.get(f"{API_URL}/api/download/{st.session_state.session_id}")
    st.download_button(
        "Download labeled customers CSV",
        data=resp.content,
        file_name="segmented_customers.csv",
        mime="text/csv",
        type="primary",
    )


def run_chat(question: str) -> None:
    st.session_state.chat_history.append({"role": "user", "content": question})
    with st.spinner("Thinking..."):
        answer = api_post(f"/api/chat/{st.session_state.session_id}", json={"question": question})
    st.session_state.chat_history.append({"role": "assistant", "answer": answer})


def render_chat() -> None:
    st.subheader("7 — Ask questions about your customers")
    st.caption("Try an example:")

    example_prompts = [
        "How many VIPs?",
        "Which segment has the most revenue?",
        "How many customers are at risk?",
    ]
    cols = st.columns(len(example_prompts))
    for col, prompt in zip(cols, example_prompts):
        if col.button(prompt, key=f"ex_{prompt}"):
            run_chat(prompt)

    for msg in st.session_state.chat_history:
        if msg["role"] == "user":
            st.chat_message("user").write(msg["content"])
        else:
            ans = msg["answer"]
            with st.chat_message("assistant"):
                if ans.get("error"):
                    st.error(ans["error"])
                elif ans.get("out_of_scope"):
                    st.info(ans["explanation"])
                else:
                    st.write(ans["explanation"])
                    if ans.get("result") is not None:
                        st.write(ans["result"])
                    if ans.get("code"):
                        with st.expander("Show pandas code"):
                            st.code(ans["code"], language="python")

    question = st.chat_input("Ask about your segments, e.g. 'how many VIPs?'")
    if question:
        run_chat(question)
        st.rerun()


def main() -> None:
    init_state()
    st.title("E-Commerce Customer Segmentation")
    st.caption(
        "Upload a messy CSV/Excel export, auto-map columns, clean, segment "
        "customers, and get recommended actions."
    )

    if not has_api_key():
        st.warning(
            "No OpenRouter API key found. Set OPENROUTER_API_KEY as an "
            "environment variable on the backend before analyzing data."
        )

    with st.sidebar:
        st.header("1 — Upload data")
        uploaded = st.file_uploader(
            "CSV or Excel export",
            type=["csv", "tsv", "txt", "xlsx", "xls"],
            help="Shopify, WooCommerce, or any custom export.",
        )

    if uploaded is None:
        st.info("Upload a file in the sidebar to begin.")
        return

    if st.session_state.uploaded_name != uploaded.name:
        reset_downstream()
        with st.spinner("Reading file and auto-mapping columns with the LLM..."):
            files = {"file": (uploaded.name, uploaded.getvalue())}
            data = api_post("/api/upload", files=files)

        st.session_state.session_id = data["session_id"]
        st.session_state.raw_preview = data["raw_preview"]
        st.session_state.raw_shape = data["raw_shape"]
        st.session_state.mapping_report = data["report"]
        st.session_state.editable_mapping = data["editable_mapping"]
        st.session_state.field_choices = data["field_choices"]
        st.session_state.uploaded_name = uploaded.name

    st.subheader("Raw data preview")
    st.write(
        f"{uploaded.name} — {st.session_state.raw_shape[0]} rows, "
        f"{st.session_state.raw_shape[1]} columns"
    )
    st.dataframe(pd.DataFrame(st.session_state.raw_preview), use_container_width=True)

    if not st.session_state.mapping_confirmed:
        render_schema_confirm()
        return

    render_validation()
    render_clustering()

    if st.session_state.cluster_out is None:
        return

    render_actions()

    if st.session_state.labeled is not None:
        render_revenue_impact()
        render_download()
        render_chat()


if __name__ == "__main__":
    main()