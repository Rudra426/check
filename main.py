from __future__ import annotations
import asyncio
import tempfile
import uuid
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from src.schema_mapper import run_schema_mapping, finalize_mapping
from src.cleaner import clean_data
from src.features import engineer_features
from src.cluster import run_clustering
from src.personas import label_customers
from src.revenue import compute_revenue_concentration, compute_clv_at_risk
from src.chat import answer_question
from config import AT_RISK_PATTERNS

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ProcessPoolExecutor(max_workers=2)
cluster_semaphore = asyncio.Semaphore(2)

SESSIONS: dict[str, dict] = {}
MAX_UPLOAD_MB = 20


def _run_cluster_sync(clean_df: pd.DataFrame) -> dict:
    features = engineer_features(clean_df)
    return run_clustering(features, save=True)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(400, f"File exceeds {MAX_UPLOAD_MB}MB limit ({size_mb:.1f}MB).")

    suffix = Path(file.filename).suffix or ".csv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        session = run_schema_mapping(Path(tmp_path))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {"mapping_session": session}

    return {
        "session_id": session_id,
        "raw_preview": session["raw_df"].head(10).to_dict(orient="records"),
        "raw_shape": list(session["raw_df"].shape),
        "report": session["report"],
        "editable_mapping": session["editable_mapping"],
        "field_choices": session["field_choices"],
    }


@app.post("/api/finalize_mapping/{session_id}")
def finalize(session_id: str, overrides: dict = None):
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found.")
    session = s["mapping_session"]
    try:
        result = finalize_mapping(session["raw_df"], session["mappings"], overrides or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if result["status"] != "rejected":
        s["normalized_df"] = result["normalized_df"]
        for key in ("clean_df", "report", "cluster_out", "labeled", "segments"):
            s.pop(key, None)

    return {
        "status": result["status"],
        "report": result["report"],
        "shape": list(result["normalized_df"].shape) if result["normalized_df"] is not None else None,
    }


@app.post("/api/clean/{session_id}")
def clean(session_id: str):
    s = SESSIONS.get(session_id)
    if not s or "normalized_df" not in s:
        raise HTTPException(404, "Confirm the column mapping first.")
    clean_df, report = clean_data(s["normalized_df"])
    s["clean_df"] = clean_df
    s["report"] = report
    return {"report": report}


@app.post("/api/cluster/{session_id}")
async def cluster(session_id: str):
    s = SESSIONS.get(session_id)
    if not s or "clean_df" not in s:
        raise HTTPException(404, "Clean the data first.")
    if s["clean_df"].empty:
        raise HTTPException(400, "No rows survived cleaning — cannot segment.")

    async with cluster_semaphore:
        loop = asyncio.get_event_loop()
        cluster_out = await loop.run_in_executor(executor, _run_cluster_sync, s["clean_df"])

    s["cluster_out"] = cluster_out
    s["labeled"] = None
    s["segments"] = None

    result_df = cluster_out["result"].copy()
    result_df["cluster"] = result_df["cluster"].astype(str)

    return {
        "metrics": cluster_out["metrics"],
        "chart_data": result_df.reset_index().to_dict(orient="records"),
    }


@app.post("/api/label/{session_id}")
def label(session_id: str):
    s = SESSIONS.get(session_id)
    if not s or "cluster_out" not in s:
        raise HTTPException(404, "Run clustering first.")
    labeled_out = label_customers(s["cluster_out"]["result"])
    s["labeled"] = labeled_out["labeled"]
    s["segments"] = labeled_out["segments"]
    return {"segments": labeled_out["segments"]}


@app.get("/api/revenue/{session_id}")
def revenue(session_id: str):
    s = SESSIONS.get(session_id)
    if not s or s.get("labeled") is None:
        raise HTTPException(404, "Label the segments first.")
    df = s["labeled"].reset_index()
    conc = compute_revenue_concentration(df, "persona", "monetary")
    atrisk = compute_clv_at_risk(df, "persona", "clv", AT_RISK_PATTERNS)

    return {
        "concentration": conc.to_dict(orient="records"),
        "at_risk": atrisk,
        "total_revenue": float(conc.attrs.get("total_revenue", 0)),
        "total_customers": int(conc.attrs.get("total_customers", 0)),
        "excluded_count": conc.attrs.get("excluded_count", 0),
    }


@app.get("/api/download/{session_id}")
def download(session_id: str):
    s = SESSIONS.get(session_id)
    if not s or s.get("labeled") is None:
        raise HTTPException(404, "Label the segments first.")
    csv_bytes = s["labeled"].reset_index().to_csv(index=False).encode("utf-8")
    return Response(
        csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=segmented_customers.csv"},
    )


@app.post("/api/chat/{session_id}")
def chat(session_id: str, payload: dict):
    s = SESSIONS.get(session_id)
    if not s or s.get("labeled") is None:
        raise HTTPException(404, "Label the segments first.")
    df = s["labeled"].reset_index()
    question = payload.get("question", "")
    answer = answer_question(df, question)
    return answer


@app.get("/api/health")
def health():
    return {"status": "ok", "active_sessions": len(SESSIONS)}