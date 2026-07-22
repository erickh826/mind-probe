"""Recording ingest + FFmpeg remux (spec §10).

Student browsers upload MediaRecorder chunks (WebM, ~2s each) over HTTP.
The server saves them in sequence, then after the session merges them and
runs FFmpeg to remux/transcode to mp4 (H.264/AAC) for teacher review
(審閱後修訂 4). These are stubs for the init scaffold.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

SESSIONS_DIR = Path(os.environ.get("MINDPROBE_SESSIONS_DIR", "./sessions"))

router = APIRouter(prefix="/recording", tags=["recording"])


def chunk_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id / "chunks"


@router.post("/{session_id}/chunk")
async def upload_chunk(
    session_id: str,
    sequence: int = Form(...),
    chunk: UploadFile = File(...),
) -> dict:
    """Receive one MediaRecorder chunk and store it by sequence (spec §10.1).

    TODO: record RecordingChunk metadata (size, checksum, upload_status),
    verify the first header chunk exists, and ack so the client can clear
    its IndexedDB queue (審閱後修訂 5).
    """
    dest = chunk_dir(session_id)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{sequence:06d}.webm"
    data = await chunk.read()
    path.write_bytes(data)
    return {"session_id": session_id, "sequence": sequence, "size_bytes": len(data)}


def concat_chunks(session_id: str) -> Path:
    """Concatenate stored WebM chunks in sequence order into one file (stub)."""
    src = chunk_dir(session_id)
    merged = SESSIONS_DIR / session_id / "student_raw.webm"
    with merged.open("wb") as out:
        for part in sorted(src.glob("*.webm")):
            out.write(part.read_bytes())
    return merged


def remux_to_mp4(session_id: str) -> Path:
    """Remux/transcode the merged WebM to mp4 (H.264/AAC) via FFmpeg (stub).

    See spec §10.1: verify duration + a/v tracks before clearing browser cache.
    """
    merged = SESSIONS_DIR / session_id / "student_raw.webm"
    out = SESSIONS_DIR / session_id / "student.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(merged),
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-c:a",
        "aac",
        str(out),
    ]
    # TODO: run and capture output; validate result before returning.
    # subprocess.run(cmd, check=True, capture_output=True)
    _ = subprocess  # referenced so linters keep the import for the real impl
    _ = cmd
    return out
