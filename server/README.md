# mind-probe Session Server

FastAPI backend for the mind-probe interrogation training simulator. Owns all
local storage (SQLite + `cases/` + `sessions/`), real-time WebSocket commands,
recording ingest, and FFmpeg remux. See `../docs/spec.md` §4–5, §9–10.

## Run

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8000
# or with pip:
pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000
```

## Test

```bash
uv run pytest        # or: pytest
```

## Layout

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, CORS, `/health`, case REST routes |
| `app/websocket.py` | `/ws/{session_id}/{role}` + connection manager |
| `app/events.py` | Pydantic event envelope + clip manifest schema (§9, §12) |
| `app/models.py` | SQLAlchemy models (Session, Event, RecordingChunk) on SQLite |
| `app/cases.py` | Loader for `cases/<case_id>/*.json` |
| `app/recording.py` | MediaRecorder chunk ingest + FFmpeg remux stub (§10) |
