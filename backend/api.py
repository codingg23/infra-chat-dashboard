"""
api.py

FastAPI backend for the infra chat dashboard.

Endpoints:
  POST /query        -  submit a natural language query, get a response
  GET  /health       -  liveness check
  GET  /metrics      -  quick summary of current facility state

Streaming via SSE for the query endpoint  -  lets the frontend show
the response as it's generated rather than waiting for the full thing.
Makes it feel much more responsive.
"""
import os
import json
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio

from .agent import InfraAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Infra Chat API", version="0.1.0")

# CORS  -  wide open for dev, lock this down before any real deployment
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_PATH = os.environ.get("DATA_PATH", "./data/")
agent = InfraAgent(data_path=DATA_PATH)


class QueryRequest(BaseModel):
    message: str
    session_id: str = "default"   # placeholder for future session memory


class QueryResponse(BaseModel):
    response: str
    session_id: str


@app.get("/health")
def health():
    return {"status": "ok", "data_path": DATA_PATH}


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    logger.info(f"Query [{req.session_id}]: {req.message[:100]}")
    try:
        response = agent.query(req.message)
        return QueryResponse(response=response, session_id=req.session_id)
    except Exception as e:
        logger.exception("Query failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics/summary")
def metrics_summary():
    """
    Quick facility summary  -  used by the dashboard header cards.
    Returns current state without needing a natural language query.
    """
    from .tools import InfraQueryTools
    from datetime import datetime, timedelta
    tools = InfraQueryTools(DATA_PATH)
    now = datetime.utcnow()
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    now_iso = now.isoformat()

    power = tools.query_power(one_hour_ago, now_iso, "facility", "facility_kw", "avg")
    thermal = tools.query_thermal(one_hour_ago, now_iso, "facility", "inlet_temp_c", "avg")
    anomalies = tools.query_anomalies(one_hour_ago, now_iso, "warning")

    return {
        "facility_kw": power.data[0]["value"] if power.data else None,
        "avg_inlet_c": thermal.data[0]["value"] if thermal.data else None,
        "active_warnings": len(anomalies.data) if anomalies.data else 0,
        "as_of": now_iso,
    }
