# mind-probe Session Server

_Updated by: Codex | Phase: Docs/Integration | Date: 2026-10-26 18:19_

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
| `app/jobs.py` | Durable single-worker processing, deletion, reconciliation and backups |
| `app/storage.py` | Local and S3-compatible private storage and signed playback |

## Background recording work

Run exactly one app process/uvicorn worker with persistent SQLite, sessions and
objects directories. Jobs are enabled by default through FastAPI lifespan;
`MINDPROBE_JOB_WORKER=false` is for deterministic tests or maintenance only.
Processing routes return 202 while the worker runs. The Teacher obtains a
15-minute signed MP4 URL and can delete through a strict teacher review grant.
See [contracts, operations and QA](../docs/jobs-storage-delete.md). No cloud
resources are created by installing or testing the local backend.
