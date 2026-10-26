"""Recording ingest + concat + FFmpeg remux (spec §10.1, ADR-0004 §4).

Student browsers upload MediaRecorder chunks (WebM, ~2s each) over HTTP. The
server verifies and stores each chunk by sequence, records its metadata in
SQLite, then -- after the session -- merges the chunks in sequence order and
runs FFmpeg to transcode the result to mp4 (H.264/AAC) for teacher review
(審閱後修訂 4).

Layout under ``SESSIONS_DIR`` (spec §10.1)::

    sessions/{session_id}/chunks/{sequence:06d}.webm   raw MediaRecorder slices
    sessions/{session_id}/student_raw.webm             concatenated raw stream
    sessions/{session_id}/student.mp4                  final transcode

**Data-retention priority (ADR-0004 §4, ADR-0005 §7.4).** Raw events rank
above raw WebM, which ranks above the final mp4, which ranks above generated
reports. Nothing in this module ever deletes a chunk or ``student_raw.webm``
-- not on a successful transcode, not when a take is ``incomplete``, and least
of all on a failed one. The only file removed here is an mp4 FFmpeg left
behind truncated, because an invalid file at the *lowest* priority level is
worse than no file at all. A take becomes deletable only after it is
``complete`` and the mp4 has been verified; that cleanup is a later job and
is not performed here.

**Failure surfaces are split by layer.** The library functions raise
(:class:`FFmpegUnavailable`, :class:`RemuxFailed`, ...) so a caller can tell a
missing binary from a broken stream. The ``POST /{session_id}/process`` route
catches those and degrades to ``status: "raw_only"``: a machine without FFmpeg
must still finish a session with a complete merged WebM on disk rather than a
500 and a half-processed take.

``SESSIONS_DIR`` is resolved through the helpers below on every call rather
than captured in a default argument -- tests redirect the whole recording root
by rebinding this module attribute.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DBSession

from . import auth, models, recording_integrity
from .models import get_db

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path(os.environ.get("MINDPROBE_SESSIONS_DIR", "./sessions"))

#: Sequence of the chunk carrying the WebM/EBML header. MediaRecorder emits it
#: once, at the start of a take, so every later chunk is undecodable without it
#: (spec §10.1).
HEADER_SEQUENCE = 0

#: First four bytes of any WebM/Matroska stream ("EBML" magic).
EBML_MAGIC = b"\x1a\x45\xdf\xa3"

CHUNK_SUFFIX = ".webm"
RAW_FILENAME = "student_raw.webm"
MP4_FILENAME = "student.mp4"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: Appended *after* the real suffix so a half-written file can never be picked
#: up by a ``*.webm`` glob, nor parsed as a sequence number.
PARTIAL_SUFFIX = ".part"

router = APIRouter(prefix="/recording", tags=["recording"])


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RecordingError(RuntimeError):
    """Base class for recording-pipeline failures."""


class NoChunksStored(RecordingError):
    """Nothing has been uploaded for this session yet."""


class MissingHeaderChunk(RecordingError):
    """Sequence 0 is absent, so the merged stream would carry no EBML header."""


class MergedStreamMissing(RecordingError):
    """``student_raw.webm`` does not exist; run :func:`concat_chunks` first."""


class FFmpegUnavailable(RecordingError):
    """The FFmpeg binary could not be executed (not installed / not on PATH)."""


class RemuxFailed(RecordingError):
    """FFmpeg ran but did not produce a usable mp4."""


class InvalidSessionId(RecordingError, ValueError):
    """The session id cannot be safely used as one filesystem segment."""


# ---------------------------------------------------------------------------
# Paths + binaries (all resolved at call time)
# ---------------------------------------------------------------------------


def validate_session_id(session_id: str) -> str:
    """Return ``session_id`` when it is safe as a storage directory name.

    Recording routes intentionally accept chunks even before a session row is
    present, so the database cannot be the first line of defense for path
    safety. Session IDs are one directory segment only: no slashes, backslashes,
    drive prefixes, ``.``/``..`` components, or percent-decoded traversal.
    """
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise InvalidSessionId(
            "session_id must start with a letter or digit and contain only "
            "letters, digits, underscore, or hyphen"
        )
    return session_id


def _sessions_root() -> Path:
    return SESSIONS_DIR.resolve(strict=False)


def _safe_session_dir(session_id: str) -> Path:
    safe_id = validate_session_id(session_id)
    root = _sessions_root()
    candidate = (root / safe_id).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise InvalidSessionId(
            f"session_id {session_id!r} resolves outside the recording root"
        ) from exc
    return candidate


def session_dir(session_id: str) -> Path:
    return _safe_session_dir(session_id)


def chunk_dir(session_id: str) -> Path:
    return session_dir(session_id) / "chunks"


def chunk_path(session_id: str, sequence: int) -> Path:
    """Zero-padded so the on-disk name sorts the same way the sequence does."""
    return chunk_dir(session_id) / f"{sequence:06d}{CHUNK_SUFFIX}"


def merged_path(session_id: str) -> Path:
    return session_dir(session_id) / RAW_FILENAME


def mp4_path(session_id: str) -> Path:
    return session_dir(session_id) / MP4_FILENAME


def _ffmpeg_bin() -> str:
    return os.environ.get("MINDPROBE_FFMPEG_BIN", "ffmpeg")


def _ffprobe_bin() -> str:
    return os.environ.get("MINDPROBE_FFPROBE_BIN", "ffprobe")


def _invalid_session_id_response(exc: InvalidSessionId) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": "invalid_session_id", "message": str(exc)},
    )


def _relative_to_sessions(path: Path) -> str:
    """A path as the API reports it -- relative to the recording root.

    Absolute server paths are not the client's business; the teacher app only
    needs enough to address the artefact.
    """
    try:
        return path.resolve(strict=False).relative_to(_sessions_root()).as_posix()
    except ValueError:
        return path.name


# ---------------------------------------------------------------------------
# Checksums + atomic writes
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Digest of a stored chunk, read in blocks so size is not a constraint."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_checksum(value: str | None) -> str | None:
    """Client-supplied digests are compared case-insensitively."""
    if value is None:
        return None
    return value.strip().lower() or None


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file plus a rename.

    A chunk is either fully on disk under its final name or not there at all,
    so a crash mid-upload cannot leave a truncated slice that
    :func:`concat_chunks` would silently splice into the merged stream.
    ``os.replace`` rather than ``Path.rename``: the latter raises on Windows
    when the destination already exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + PARTIAL_SUFFIX)
    temp.write_bytes(data)
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# Chunk inventory (spec §10.1)
# ---------------------------------------------------------------------------


def stored_chunks(session_id: str) -> list[tuple[int, Path]]:
    """Every stored chunk as ``(sequence, path)``, sorted numerically.

    The sequence is parsed back out of the filename rather than inferred from
    a lexical sort: the zero padding only keeps the two orders in agreement up
    to 999999 (~23 days of 2s slices), and anything in the directory that is
    not a number is not ours to merge, so it is skipped with a warning.
    """
    directory = chunk_dir(session_id)
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in directory.glob(f"*{CHUNK_SUFFIX}"):
        try:
            sequence = int(path.stem)
        except ValueError:
            logger.warning(
                "Ignoring unrecognized file in the chunk directory of session %s: %s",
                session_id,
                path.name,
            )
            continue
        found.append((sequence, path))
    found.sort(key=lambda item: item[0])
    return found


def missing_sequences(session_id: str) -> list[int]:
    """Gaps inside the stored sequence range -- slices that never landed.

    Only interior gaps are reported. A take that begins at sequence 5 because
    the page was reloaded is a different failure, surfaced by
    :func:`validate_header_chunk` instead.
    """
    sequences = [sequence for sequence, _ in stored_chunks(session_id)]
    if not sequences:
        return []
    present = set(sequences)
    return [n for n in range(sequences[0], sequences[-1] + 1) if n not in present]


def validate_header_chunk(session_id: str) -> bool:
    """Whether sequence 0 exists, is non-empty, and starts with EBML magic.

    Checked before merging because a stream missing its header decodes to
    nothing at all -- the failure mode spec §10.1 calls out by name.
    """
    header = chunk_path(session_id, HEADER_SEQUENCE)
    if not header.is_file() or header.stat().st_size == 0:
        return False
    with header.open("rb") as handle:
        return handle.read(len(EBML_MAGIC)) == EBML_MAGIC


# ---------------------------------------------------------------------------
# Concatenation (spec §10.1)
# ---------------------------------------------------------------------------


def concat_chunks(session_id: str, *, allow_missing_header: bool = False) -> Path:
    """Concatenate stored chunks in sequence order into ``student_raw.webm``.

    MediaRecorder slices are fragments of one continuous stream, so a byte
    concatenation in the original order reproduces the recording exactly --
    *provided* the order is right. That is why chunks are addressed by the
    sequence the client assigned rather than by arrival time or mtime: uploads
    legitimately arrive out of order after a reconnect (ADR-0002 §8), and the
    merge is driven off :func:`stored_chunks`, which re-sorts numerically.

    The merge is written to a temp file and renamed into place, so an
    interrupted run leaves the previous merged stream intact rather than a
    truncated one. Source chunks are never removed (ADR-0004 §4).

    ``allow_missing_header`` is an operator escape hatch for salvaging a take
    whose first slice was lost: the result will not decode as-is, but merging
    the bytes beats stranding them.

    Raises :class:`NoChunksStored` or :class:`MissingHeaderChunk`.
    """
    chunks = stored_chunks(session_id)
    if not chunks:
        raise NoChunksStored(f"no recording chunks stored for session: {session_id}")

    if not validate_header_chunk(session_id):
        if not allow_missing_header:
            raise MissingHeaderChunk(
                f"session {session_id} has no valid header chunk "
                f"({HEADER_SEQUENCE:06d}{CHUNK_SUFFIX}); the merged stream would "
                "not decode. Pass allow_missing_header=True to merge anyway."
            )
        logger.warning(
            "Merging session %s without a valid header chunk; the result needs "
            "an external header to decode.",
            session_id,
        )

    gaps = missing_sequences(session_id)
    if gaps:
        # Not fatal: a gap costs those seconds of video, whereas refusing to
        # merge would cost the whole take (ADR-0004 §4).
        logger.warning(
            "Session %s is missing %d chunk(s) inside its sequence range: %s",
            session_id,
            len(gaps),
            gaps[:20],
        )

    merged = merged_path(session_id)
    merged.parent.mkdir(parents=True, exist_ok=True)
    temp = merged.with_name(merged.name + PARTIAL_SUFFIX)
    with temp.open("wb") as out:
        for _sequence, part in chunks:
            with part.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    out.write(block)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, merged)
    logger.info(
        "Merged %d chunk(s) of session %s into %s (%d bytes).",
        len(chunks),
        session_id,
        merged,
        merged.stat().st_size,
    )
    return merged


# ---------------------------------------------------------------------------
# FFmpeg transcode + media verification (spec §10.1)
# ---------------------------------------------------------------------------


def _stderr_tail(stderr: bytes | str | None, limit: int = 600) -> str:
    """The last of FFmpeg's stderr -- where the actual reason always is."""
    if not stderr:
        return ""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return text.strip()[-limit:]


def probe_media(path: Path, *, ffprobe_bin: str | None = None) -> dict | None:
    """Duration and stream layout of a media file, or ``None`` if unprobeable.

    Backs spec §10.1's "verify duration + a/v tracks before clearing the
    browser cache" and the M4 go/no-go duration tolerance. Returns ``None``
    rather than raising when ffprobe is absent or the file is unreadable:
    verification is a check on top of a transcode that already succeeded, not
    a reason to fail one.
    """
    binary = ffprobe_bin or _ffprobe_bin()
    cmd = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-show_entries",
        "stream=codec_type,codec_name",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError:
        logger.info("ffprobe (%s) unavailable; skipping media verification.", binary)
        return None
    except subprocess.CalledProcessError as exc:
        logger.warning("ffprobe failed for %s: %s", path, _stderr_tail(exc.stderr))
        return None

    try:
        parsed = json.loads(completed.stdout or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("ffprobe returned unparseable output for %s.", path)
        return None

    streams = parsed.get("streams") or []
    codec_types = [s.get("codec_type") for s in streams if s.get("codec_type")]
    raw_duration = (parsed.get("format") or {}).get("duration")
    try:
        duration_s = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration_s = None

    return {
        "duration_s": duration_s,
        "codec_types": codec_types,
        "codecs": [s.get("codec_name") for s in streams if s.get("codec_name")],
        "has_video": "video" in codec_types,
        "has_audio": "audio" in codec_types,
    }


def _discard_invalid_mp4(out: Path) -> None:
    """Remove an unusable mp4 FFmpeg left behind.

    Permitted under ADR-0004 §4: the mp4 is the lowest-priority artefact and
    this one is known-invalid, while every WebM it was derived from is
    untouched. Leaving it would tell the review app the take was processed.
    """
    try:
        out.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - permission/locking dependent
        logger.warning("Could not remove the incomplete transcode at %s.", out)


def remux_to_mp4(session_id: str, *, ffmpeg_bin: str | None = None, cancel_check=None) -> Path:
    """Transcode ``student_raw.webm`` to ``student.mp4`` (H.264/AAC).

    Baseline profile plus ``yuv420p`` so the result plays in every browser the
    teacher app may run in, and ``+faststart`` so the moov atom sits at the
    front and review can begin before the file is fully fetched.

    This *raises* on every failure instead of degrading, so a caller can tell
    "no FFmpeg on this box" (:class:`FFmpegUnavailable`) from "this stream is
    broken" (:class:`RemuxFailed`). Degrading is the route's job, not the
    pipeline's.

    On every failure path the chunks and ``student_raw.webm`` are left exactly
    as they were (ADR-0004 §4).
    """
    merged = merged_path(session_id)
    if not merged.is_file() or merged.stat().st_size == 0:
        raise MergedStreamMissing(
            f"no merged stream at {merged}; call concat_chunks() first"
        )

    final = mp4_path(session_id)
    out = final.with_name("student.part.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    binary = ffmpeg_bin or _ffmpeg_bin()
    cmd = [
        binary,
        "-y",
        # Never let FFmpeg inherit and then block on stdin -- this runs headless.
        "-nostdin",
        "-i",
        str(merged),
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(out),
    ]

    try:
        if cancel_check is None:
            subprocess.run(cmd, check=True, capture_output=True)
        else:
            # A real worker can stop FFmpeg promptly after delete/shutdown.
            # File-backed stderr avoids filling a pipe while polling.
            with tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr)
                try:
                    started = time.monotonic()
                    while process.poll() is None:
                        cancel_check()
                        if time.monotonic() - started > 900:
                            raise RemuxFailed("FFmpeg timeout")
                        time.sleep(0.2)
                    if process.returncode:
                        stderr.seek(0)
                        raise subprocess.CalledProcessError(process.returncode, cmd, stderr=stderr.read())
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
    except FileNotFoundError as exc:
        # Nothing ran, so nothing to clean up; the WebM is transcodable later.
        raise FFmpegUnavailable(
            f"FFmpeg binary not found: {binary!r}. The raw WebM for session "
            f"{session_id} is untouched and can be transcoded later."
        ) from exc
    except subprocess.CalledProcessError as exc:
        _discard_invalid_mp4(out)
        raise RemuxFailed(
            f"FFmpeg exited {exc.returncode} for session {session_id}: "
            f"{_stderr_tail(exc.stderr)}"
        ) from exc

    if not out.is_file() or out.stat().st_size == 0:
        _discard_invalid_mp4(out)
        raise RemuxFailed(
            f"FFmpeg reported success but produced no usable mp4 for session "
            f"{session_id}."
        )

    probe = probe_media(out)
    if probe is not None and not probe["has_video"]:
        # No video track means the source never carried a decodable one;
        # surfacing it now beats a teacher discovering it at review time.
        _discard_invalid_mp4(out)
        raise RemuxFailed(
            f"the transcoded mp4 for session {session_id} has no video stream "
            f"(streams: {probe['codec_types']})."
        )
    if probe is not None and not probe["has_audio"]:
        # Legitimate when the student had no microphone -- worth a line in the
        # log, not worth discarding the video over.
        logger.warning("Transcoded mp4 for session %s has no audio stream.", session_id)

    logger.info("Transcoded session %s to %s.", session_id, out)
    if cancel_check is not None:
        cancel_check()
    out.replace(final)
    return final


# ---------------------------------------------------------------------------
# Status + end-of-session processing
# ---------------------------------------------------------------------------


def _artefact_info(path: Path) -> dict:
    exists = path.is_file()
    return {
        "exists": exists,
        "path": _relative_to_sessions(path) if exists else None,
        "size_bytes": path.stat().st_size if exists else None,
    }


def get_recording_status(
    session_id: str,
    db: DBSession | None = None,
    *,
    include_chunks: bool = False,
) -> dict:
    """Completeness report for one session's recording (spec §14, M4 go/no-go).

    Disk is authoritative for *what exists*: ADR-0004 §4 ranks the raw WebM
    above anything derived from it, and a chunk whose metadata row failed to
    write is still a chunk the teacher can watch. The database supplies the
    facts the bytes cannot carry -- checksum, ``start_ms``/``end_ms``, upload
    status -- so the two are reconciled here rather than either being trusted
    alone. ``metadata_rows`` below ``chunk_count`` is exactly that drift.
    """
    chunks = stored_chunks(session_id)
    sequences = [sequence for sequence, _ in chunks]
    metadata: dict[int, models.RecordingChunk] = {}
    if db is not None:
        metadata = {
            row.sequence: row for row in models.get_recording_chunks(db, session_id)
        }

    status: dict = {
        "session_id": session_id,
        "chunk_count": len(chunks),
        "first_sequence": sequences[0] if sequences else None,
        "last_sequence": sequences[-1] if sequences else None,
        "missing_sequences": missing_sequences(session_id),
        "total_bytes": sum(path.stat().st_size for _seq, path in chunks),
        "header_chunk_ok": validate_header_chunk(session_id),
        "metadata_rows": len(metadata),
        "raw_webm": _artefact_info(merged_path(session_id)),
        "mp4": _artefact_info(mp4_path(session_id)),
    }
    if db is not None:
        rec = models.get_recording(db, session_id)
        if rec is not None:
            status["recording_state"] = rec.recording_state
            status["finalize_state"] = rec.finalize_state
            status["final_sequence"] = rec.final_sequence
            status["incomplete_reason"] = rec.incomplete_reason
            if rec.final_sequence is not None:
                status["missing_sequences"] = recording_integrity.missing_for_final(
                    recording_integrity._received(session_id, rec), rec.final_sequence
                )

    if include_chunks:
        # Opt-in: a 30-minute session is ~900 slices, which no caller wants
        # attached to a routine completeness poll.
        status["chunks"] = [
            {
                "sequence": sequence,
                "size_bytes": path.stat().st_size,
                "checksum": getattr(metadata.get(sequence), "checksum", None),
                "start_ms": getattr(metadata.get(sequence), "start_ms", None),
                "end_ms": getattr(metadata.get(sequence), "end_ms", None),
                "upload_status": getattr(metadata.get(sequence), "upload_status", None),
            }
            for sequence, path in chunks
        ]
    return status


def process_recording(
    session_id: str,
    db: DBSession | None = None,
    *,
    remux: bool = True,
    allow_missing_header: bool = False,
    cancel_check=None,
) -> dict:
    """Merge a session's chunks and, optionally, transcode the result.

    Returns ``status: "completed"`` once an mp4 exists, or ``"raw_only"`` when
    the merge succeeded but the transcode did not. The second is a real
    outcome rather than an error: a box without FFmpeg still has to finish the
    session holding a complete WebM that can be transcoded later (ADR-0004 §4).

    ``duration_delta_s`` is the raw-vs-mp4 difference the M4 go/no-go table
    caps at one second; it is ``None`` when ffprobe is unavailable.
    """
    merged = concat_chunks(session_id, allow_missing_header=allow_missing_header)
    raw_probe = probe_media(merged)

    result: dict = {
        "session_id": session_id,
        "status": "raw_only",
        "chunk_count": len(stored_chunks(session_id)),
        "missing_sequences": missing_sequences(session_id),
        "raw_webm": _artefact_info(merged),
        "raw_duration_s": (raw_probe or {}).get("duration_s"),
        "mp4": None,
        "mp4_duration_s": None,
        "duration_delta_s": None,
        "remux_error": None,
    }
    if db is not None:
        result["metadata_rows"] = len(models.get_recording_chunks(db, session_id))

    if not remux:
        result["remux_error"] = "skipped"
        return result

    try:
        out = remux_to_mp4(session_id, cancel_check=cancel_check) if cancel_check else remux_to_mp4(session_id)
    except (FFmpegUnavailable, RemuxFailed) as exc:
        # Both WebM artefacts survive by construction; report and move on.
        logger.warning("Transcode unavailable for session %s: %s", session_id, exc)
        result["remux_error"] = str(exc)
        return result

    mp4_probe = probe_media(out)
    raw_duration = result["raw_duration_s"]
    mp4_duration = (mp4_probe or {}).get("duration_s")
    result.update(
        {
            "status": "completed",
            "mp4": _artefact_info(out),
            "mp4_duration_s": mp4_duration,
            "duration_delta_s": (
                abs(mp4_duration - raw_duration)
                if mp4_duration is not None and raw_duration is not None
                else None
            ),
        }
    )
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/{session_id}/chunk")
async def upload_chunk(
    session_id: str,
    sequence: int = Form(...),
    start_ms: int | None = Form(None),
    end_ms: int | None = Form(None),
    checksum: str | None = Form(None),
    chunk: UploadFile = File(...),
    _actor: auth.Actor = Depends(
        auth.require_roles(
            "student",
            revocation="grace_upload",
            allowed_typs=("live", "upload"),
        )
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """Receive one MediaRecorder chunk, verify it, and store it (spec §10.1).

    The ack carries the server-computed SHA-256 so the client can confirm the
    round trip before clearing the slice from its queue (審閱後修訂 5).

    Integrity rules, in order:

    1. An empty slice is rejected -- there is no data to retain, and a
       zero-byte file would only poison the merge.
    2. A client-supplied ``checksum`` is compared *before* anything is written,
       so a corrupted upload can never clobber a good stored chunk.
    3. Re-uploading a sequence already on disk is idempotent when the bytes
       match (``status: "duplicate"``) and a 409 when they do not: under
       ADR-0004 §4 the stored raw slice outranks an unexplained second version
       of the same sequence.

    Metadata persistence is deliberately best-effort. A database hiccup must
    not cost the recording, so the file is durable before the row is written
    and a failure is reported in ``metadata_persisted`` rather than raised.
    """
    if sequence < 0:
        raise HTTPException(status_code=400, detail="sequence must be >= 0")
    try:
        validate_session_id(session_id)
    except InvalidSessionId as exc:
        raise _invalid_session_id_response(exc) from exc

    declared = models.get_recording(db, session_id)
    if declared is not None and declared.recording_state in {"deleting", "deleted"}:
        raise HTTPException(status_code=409, detail="recording_deleted")
    if (
        declared is not None
        and declared.final_sequence is not None
        and sequence > declared.final_sequence
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "sequence_past_final",
                "sequence": sequence,
                "final_sequence": declared.final_sequence,
            },
        )

    data = await chunk.read()
    if not data:
        raise HTTPException(
            status_code=400, detail=f"chunk {sequence} is empty; nothing to store"
        )

    digest = sha256_bytes(data)
    expected = _normalize_checksum(checksum)
    if expected is not None and expected != digest:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "checksum_mismatch",
                "sequence": sequence,
                "expected": expected,
                "actual": digest,
            },
        )

    path = chunk_path(session_id, sequence)
    db.expire_all()
    declared = models.get_recording(db, session_id)
    if declared and declared.recording_state in {"deleting", "deleted"}:
        raise HTTPException(status_code=409, detail="recording_deleted")
    if declared and declared.manifest:
        entry = next((row for row in declared.manifest.get("chunks", []) if row["sequence"] == sequence), None)
        if entry and entry["sha256"] == digest:
            return {"session_id": session_id, "sequence": sequence, "size_bytes": len(data),
                    "checksum": digest, "status": "duplicate", "metadata_persisted": True}
        raise HTTPException(status_code=409, detail={"error": "sequence_conflict", "sequence": sequence})
    if path.is_file():
        stored_digest = sha256_file(path)
        if stored_digest != digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "sequence_conflict",
                    "sequence": sequence,
                    "stored_checksum": stored_digest,
                    "uploaded_checksum": digest,
                },
            )
        # Same bytes: the client is retrying an ack it never saw. Leave the
        # stored file alone and re-ack it.
        upload_state = "duplicate"
    else:
        _atomic_write(path, data)
        upload_state = "received"

    if models.get_session(db, session_id) is None:
        # Not fatal: dropping the take because the session row is missing would
        # invert ADR-0004 §4's priorities. The bytes are on disk either way.
        logger.warning(
            "Recording chunk %d accepted for unknown session %s.", sequence, session_id
        )

    metadata_persisted = True
    try:
        if models.get_session(db, session_id) is not None:
            models.ensure_recording(db, session_id)
        models.save_recording_chunk_metadata(
            db,
            session_id,
            sequence,
            size_bytes=len(data),
            checksum=digest,
            start_ms=start_ms,
            end_ms=end_ms,
            path=f"sessions/{session_id}/chunks/{path.name}",
            commit=False,
        )
        db.commit()
        # A late slice can close a gap the finalize declaration was waiting on.
        # Protected incomplete takes stay incomplete (D8); nothing is deleted.
        recording_integrity.refresh_completeness(db, session_id)
    except SQLAlchemyError:
        db.rollback()
        metadata_persisted = False
        logger.exception(
            "Stored chunk %d for session %s but could not persist its metadata.",
            sequence,
            session_id,
        )

    return {
        "session_id": session_id,
        "sequence": sequence,
        "size_bytes": len(data),
        "checksum": digest,
        "status": upload_state,
        "metadata_persisted": metadata_persisted,
    }


class FinalizeRequest(BaseModel):
    """Student declaration of the last sequence in this take (ADR-0005 §7.2)."""

    model_config = ConfigDict(extra="forbid")

    final_sequence: int = Field(ge=-1)
    total_chunks: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def total_chunks_matches_final_sequence(self) -> "FinalizeRequest":
        if self.total_chunks != self.final_sequence + 1:
            raise ValueError("total_chunks must equal final_sequence + 1")
        return self


@router.get("/{session_id}/status")
def recording_status(
    session_id: str,
    _actor: auth.Actor = Depends(
        auth.require_roles(
            "student",
            revocation="grace_upload",
            allowed_typs=("live", "upload"),
        )
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """Student upload progress. Five fields, no paths or checksums (ADR-0005 §5).

    Teachers, observers, and operators use ``GET /sessions/{id}/recording/status``.
    """
    try:
        validate_session_id(session_id)
    except InvalidSessionId as exc:
        raise _invalid_session_id_response(exc) from exc
    rec = models.get_recording(db, session_id)
    if rec and rec.recording_state in {"deleting", "deleted"}:
        raise HTTPException(status_code=410, detail="recording_deleted")
    return recording_integrity.student_upload_status(session_id, db)


@router.post("/{session_id}/finalize")
def finalize_recording(
    session_id: str,
    body: FinalizeRequest,
    actor: auth.Actor = Depends(
        auth.require_roles(
            "student",
            revocation="grace_upload",
            allowed_typs=("live", "upload"),
        )
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """Idempotent end-of-take declaration (ADR-0005 §7.2, D8, D9).

    The same payload returns 200 and the current ``finalize_state`` plus
    ``missing_sequences``. ``reason=recorder_lost`` latches ``incomplete`` and
    is not cleared by a later full chunk set.
    """
    if body.reason not in {None, "recorder_lost"}:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_finalize_reason", "reason": body.reason},
        )
    try:
        validate_session_id(session_id)
    except InvalidSessionId as exc:
        raise _invalid_session_id_response(exc) from exc
    return recording_integrity.apply_finalize(
        db,
        session_id,
        final_sequence=body.final_sequence,
        total_chunks=body.total_chunks,
        total_duration_ms=body.total_duration_ms,
        reason=body.reason,
        actor_role=actor.role,
        token_hash=actor.token_hash,
    )


@router.post("/{session_id}/process", status_code=202)
def process_session_recording(
    session_id: str,
    remux: bool = True,
    allow_missing_header: bool = False,
    _actor: auth.Actor = Depends(auth.require_recording_delete_actor),
    db: DBSession = Depends(get_db),
) -> dict:
    """Enqueue processing only; FFmpeg and filesystem cleanup run in the worker.

    Incomplete/recorder_lost takes are refused even for OP. Raw-only processing
    is no longer a request operation. Partial salvage has no implemented path.
    """
    try:
        validate_session_id(session_id)
    except InvalidSessionId as exc:
        raise _invalid_session_id_response(exc) from exc
    if not recording_integrity.transcode_allowed(session_id, db):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "recording_not_complete",
                "message": (
                    "transcode runs only for a complete recording; "
                    "incomplete takes keep their raw chunks"
                ),
            },
        )
    if allow_missing_header and not _actor.is_operator:
        raise HTTPException(status_code=403, detail="operator_required")
    if not remux:
        raise HTTPException(status_code=400, detail="remux_required")
    from .jobs import enqueue
    rec = models.get_recording(db, session_id)
    kind = "upload" if rec and rec.recording_state in {"transcoded", "uploaded"} else "transcode"
    job_id = enqueue(db, kind, session_id)
    db.commit()
    return {"session_id": session_id, "status": "queued", "job_id": job_id}
