"""FastAPI application entrypoint (spec §4, §5).

Wires up CORS (LAN dev), a health check, the WebSocket endpoint, the
recording ingest routes, and read-only case REST endpoints used by the
teacher review app (審閱後修訂 3).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, cases
from .models import init_db
from .recording import router as recording_router
from .websocket import router as websocket_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="mind-probe Session Server", version=__version__, lifespan=lifespan)

# MVP runs on one LAN; allow all origins in dev. Tighten for pilot.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(websocket_router)
app.include_router(recording_router)


@app.get("/health")
def health() -> dict:
    """Liveness/readiness check."""
    return {"status": "ok", "service": "mind-probe-server", "version": __version__}


@app.get("/cases")
def get_cases() -> dict:
    return {"cases": cases.list_cases()}


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    try:
        return cases.load_case(case_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")


@app.get("/cases/{case_id}/clips")
def get_case_clips(case_id: str) -> dict:
    try:
        clips = cases.load_clip_manifest(case_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")
    return {"case_id": case_id, "clips": [c.model_dump() for c in clips]}
