"""Recording ingest, concat and remux tests (spec §10.1; ADR-0004 §4).

Covers the Server/Media Agent slice of `app/recording.py`:

  - chunk ingest: ack shape, SHA-256 verification, idempotent retries,
    overwrite protection, SQLite metadata;
  - `concat_chunks`: numeric (not lexical) sequence ordering, header-chunk
    validation, atomic replacement;
  - `remux_to_mp4`: the FFmpeg command line, the two distinct failure modes,
    and -- crucially -- that neither of them costs a byte of WebM
    (ADR-0004 §4's retention priority);
  - the REST surface: `/status` and `/process`, including the `raw_only`
    degradation on a box with no FFmpeg.

Every test runs against its own in-memory SQLite engine (injected through the
`get_db` dependency) and its own scratch recording root, so nothing here
touches `sessions/mind-probe.sqlite` or the real `server/sessions/`.

The failure paths fake `subprocess.run` so they run anywhere. One end-to-end
test drives the *real* FFmpeg and is skipped when the binary is absent; it is
the only place the actual H.264/AAC output and the M4 go/no-go duration
tolerance are verified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Set before app.main imports app.websocket, which reads it when signing.
# setdefault so a secret an earlier test module set is never clobbered.
os.environ.setdefault("MINDPROBE_WS_TOKEN_SECRET", "test-recording-secret")

from app import models, recording, websocket
from app.main import app
from app.models import Base

OPERATOR_HEADERS = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}

#: A minimal but genuine WebM/EBML header, so `validate_header_chunk` is
#: exercised against the magic bytes a real MediaRecorder emits rather than a
#: placeholder that would pass a weaker check.
EBML_HEADER = recording.EBML_MAGIC + b"\x01\x00\x00\x00\x00\x00\x00\x23header"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe not installed on this machine",
)


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def db_factory():
    """A sessionmaker bound to a private in-memory database.

    StaticPool keeps every session on one connection, so the `:memory:`
    database is shared with the thread TestClient runs handlers on.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_factory):
    """TestClient with `get_db` pointed at the test database.

    Deliberately not entered as a context manager: the lifespan would run
    `init_db()` against the real engine (same reasoning as
    `test_persistence.py`'s client fixture).
    """

    def _override_get_db():
        session = db_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[models.get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(models.get_db, None)


@pytest.fixture()
def chunk_root(monkeypatch):
    """A throwaway recording root, pointed at by `recording.SESSIONS_DIR`.

    Deliberately not pytest's `tmp_path`, matching `test_m1_e2e.py`: that lives
    under the system temp directory, which is not always writable in the
    environments this suite runs in. Removed on teardown so a run leaves the
    working tree as it found it.
    """
    root = Path(__file__).resolve().parent / ".tmp_recordings" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(recording, "SESSIONS_DIR", root)
    yield root
    shutil.rmtree(root, ignore_errors=True)
    try:
        root.parent.rmdir()
    except OSError:
        pass  # another test's scratch space is still in there


# --- Helpers ----------------------------------------------------------------


def _new_session(db, case_id: str = "CASE001") -> str:
    """Create a persisted session so chunk metadata has a parent row."""
    session_id = f"S-{uuid4().hex[:8]}"
    models.create_session(db, session_id, case_id)
    return session_id


def _student_headers(session_id: str) -> dict[str, str]:
    token = websocket.create_session_token(session_id, "student")
    return {"Authorization": f"Bearer {token}"}


def _post_chunk(client, session_id: str, sequence: int, data: bytes, **form):
    return client.post(
        f"/recording/{session_id}/chunk",
        data={"sequence": sequence, **form},
        files={"chunk": (f"{sequence:06d}.webm", data, "video/webm")},
        headers=_student_headers(session_id),
    )


def _mark_complete(db, session_id: str, *, final_sequence: int, total_chunks: int) -> None:
    """Let the existing transcode route run. It now requires a complete take."""
    rec = models.ensure_recording(db, session_id)
    assert rec is not None
    rec.recording_state = "complete"
    rec.finalize_state = "complete"
    rec.final_sequence = final_sequence
    rec.total_chunks = total_chunks
    rec.total_duration_ms = 0
    rec.missing_sequences = []
    db.commit()


def _write_chunks(session_id: str, payloads: dict[int, bytes]) -> None:
    """Place chunks on disk directly, bypassing the upload route."""
    directory = recording.chunk_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    for sequence, data in payloads.items():
        recording.chunk_path(session_id, sequence).write_bytes(data)


def _chunk_files(session_id: str) -> list[str]:
    return sorted(p.name for p in recording.chunk_dir(session_id).glob("*"))


def _failing_run(returncode: int = 1, stderr: bytes = b"boom"):
    """A `subprocess.run` stand-in that reports an FFmpeg failure."""

    def _run(cmd, *args, **kwargs):
        raise subprocess.CalledProcessError(returncode, cmd, b"", stderr)

    return _run


def _missing_binary_run(cmd, *args, **kwargs):
    """A `subprocess.run` stand-in for a machine with no FFmpeg installed."""
    raise FileNotFoundError(2, "No such file or directory", cmd[0])


# --- Chunk ingest (spec §10.1) ----------------------------------------------


def test_upload_chunk_stores_file_and_acks_with_checksum(client, db, chunk_root):
    """The ack tells the client exactly what the server believes it stored."""
    session_id = _new_session(db)
    payload = EBML_HEADER + b"-slice-0"

    response = _post_chunk(client, session_id, 0, payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == session_id
    assert body["sequence"] == 0
    assert body["size_bytes"] == len(payload)
    assert body["checksum"] == recording.sha256_bytes(payload)
    assert body["status"] == "received"
    assert body["metadata_persisted"] is True

    stored = recording.chunk_path(session_id, 0)
    assert stored.read_bytes() == payload
    # Zero-padded so the on-disk order tracks the sequence order.
    assert stored.name == "000000.webm"


def test_upload_chunk_persists_metadata_to_sqlite(client, db, chunk_root):
    """Sequence, size, checksum and path all reach `recording_chunks`."""
    session_id = _new_session(db)
    payloads = {0: EBML_HEADER, 1: b"slice-one", 2: b"slice-two-longer"}
    for sequence, data in payloads.items():
        response = _post_chunk(
            client,
            session_id,
            sequence,
            data,
            start_ms=sequence * 2000,
            end_ms=(sequence + 1) * 2000,
        )
        assert response.status_code == 200, response.text

    rows = models.get_recording_chunks(db, session_id)
    assert [row.sequence for row in rows] == [0, 1, 2]
    assert [row.size_bytes for row in rows] == [len(p) for p in payloads.values()]
    assert [row.checksum for row in rows] == [
        recording.sha256_bytes(p) for p in payloads.values()
    ]
    assert [row.start_ms for row in rows] == [0, 2000, 4000]
    assert [row.end_ms for row in rows] == [2000, 4000, 6000]
    assert {row.upload_status for row in rows} == {"received"}
    assert all(row.path and row.path.endswith(".webm") for row in rows)


def test_upload_chunk_accepts_a_matching_client_checksum(client, db, chunk_root):
    session_id = _new_session(db)
    payload = b"verified-slice"

    response = _post_chunk(
        client,
        session_id,
        3,
        payload,
        # Upper case on purpose: digests are compared case-insensitively.
        checksum=recording.sha256_bytes(payload).upper(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["checksum"] == recording.sha256_bytes(payload)


def test_checksum_mismatch_is_rejected_without_touching_stored_data(
    client, db, chunk_root
):
    """A corrupted upload is refused *before* anything is written.

    The stored slice for that sequence has to survive intact -- overwriting it
    with bytes that failed verification would destroy raw media to make room
    for garbage (ADR-0004 §4).
    """
    session_id = _new_session(db)
    good = EBML_HEADER + b"-good"
    assert _post_chunk(client, session_id, 0, good).status_code == 200

    response = _post_chunk(
        client, session_id, 0, b"corrupted-bytes", checksum=recording.sha256_bytes(good)
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "checksum_mismatch"
    assert detail["sequence"] == 0
    assert recording.chunk_path(session_id, 0).read_bytes() == good
    row = models.get_recording_chunks(db, session_id)[0]
    assert row.checksum == recording.sha256_bytes(good)


def test_reuploading_identical_bytes_is_idempotent(client, db, chunk_root):
    """A retried upload (lost ack) must not duplicate the file or the row."""
    session_id = _new_session(db)
    payload = EBML_HEADER + b"-retry"

    first = _post_chunk(client, session_id, 0, payload)
    second = _post_chunk(client, session_id, 0, payload)

    assert first.json()["status"] == "received"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["checksum"] == first.json()["checksum"]
    assert _chunk_files(session_id) == ["000000.webm"]
    assert len(models.get_recording_chunks(db, session_id)) == 1


def test_conflicting_bytes_for_a_stored_sequence_are_refused(client, db, chunk_root):
    """Overwrite protection: the slice already on disk wins (ADR-0004 §4)."""
    session_id = _new_session(db)
    original = EBML_HEADER + b"-original"
    assert _post_chunk(client, session_id, 0, original).status_code == 200

    response = _post_chunk(client, session_id, 0, b"a-different-take")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "sequence_conflict"
    assert detail["stored_checksum"] == recording.sha256_bytes(original)
    assert detail["uploaded_checksum"] == recording.sha256_bytes(b"a-different-take")
    assert recording.chunk_path(session_id, 0).read_bytes() == original


def test_empty_and_negative_chunks_are_rejected(client, db, chunk_root):
    """Neither can be merged, and a zero-byte file would poison the concat."""
    session_id = _new_session(db)

    assert _post_chunk(client, session_id, 0, b"").status_code == 400
    assert _post_chunk(client, session_id, -1, b"data").status_code == 400
    assert not recording.chunk_dir(session_id).exists()


def test_chunk_upload_survives_an_unknown_session(client, chunk_root):
    """Media outranks bookkeeping: an orphan chunk is still stored.

    Refusing the upload because the session row is missing would invert
    ADR-0004 §4 -- the bytes are irreplaceable, the row is not.
    """
    response = _post_chunk(client, "S-never-created", 0, EBML_HEADER)

    assert response.status_code == 200
    assert recording.chunk_path("S-never-created", 0).exists()


def test_no_partial_files_are_left_behind(client, db, chunk_root):
    """Atomic write: the `.part` temp never survives a completed upload."""
    session_id = _new_session(db)
    assert _post_chunk(client, session_id, 0, EBML_HEADER).status_code == 200

    assert _chunk_files(session_id) == ["000000.webm"]


def test_path_helpers_reject_session_ids_that_escape_the_recording_root(chunk_root):
    """The storage root is a hard boundary even for direct library callers."""
    bad_ids = ("..", "../escape", r"..\escape", "/absolute", r"C:\absolute", "S001/extra")

    for session_id in bad_ids:
        with pytest.raises(recording.InvalidSessionId):
            recording.chunk_path(session_id, 0)

    assert list(chunk_root.iterdir()) == []


def test_upload_rejects_path_like_session_ids_before_writing(client, chunk_root):
    """Unknown sessions are accepted; path-shaped session IDs are not."""
    response = _post_chunk(client, r"..\escape", 0, EBML_HEADER)

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_session_id"
    assert list(chunk_root.iterdir()) == []


def test_status_and_process_reject_invalid_session_ids(client, chunk_root):
    student = _student_headers("bad!id")
    response = client.get("/recording/bad%21id/status", headers=student)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_session_id"
    response = client.post("/recording/bad%21id/process", headers=OPERATOR_HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_session_id"


# --- Header validation + inventory ------------------------------------------


def test_validate_header_chunk_requires_a_real_ebml_header(chunk_root):
    missing = "S-no-header"
    _write_chunks(missing, {1: b"body"})
    assert recording.validate_header_chunk(missing) is False

    empty = "S-empty-header"
    _write_chunks(empty, {0: b""})
    assert recording.validate_header_chunk(empty) is False

    not_webm = "S-not-webm"
    _write_chunks(not_webm, {0: b"this is not a webm stream"})
    assert recording.validate_header_chunk(not_webm) is False

    good = "S-good-header"
    _write_chunks(good, {0: EBML_HEADER})
    assert recording.validate_header_chunk(good) is True


def test_missing_sequences_reports_interior_gaps_only(chunk_root):
    session_id = "S-gaps"
    _write_chunks(session_id, {0: EBML_HEADER, 1: b"a", 3: b"c", 6: b"f"})

    assert recording.missing_sequences(session_id) == [2, 4, 5]
    assert recording.missing_sequences("S-nothing-here") == []


def test_stored_chunks_sorts_numerically_not_lexically(chunk_root):
    """Seven-digit sequences must not sort before six-digit ones."""
    session_id = "S-wide"
    _write_chunks(session_id, {9: b"nine", 10: b"ten", 1000000: b"million"})

    assert [seq for seq, _ in recording.stored_chunks(session_id)] == [9, 10, 1000000]


def test_stored_chunks_ignores_files_that_are_not_sequences(chunk_root):
    session_id = "S-junk"
    _write_chunks(session_id, {0: EBML_HEADER})
    (recording.chunk_dir(session_id) / "notes.webm").write_bytes(b"not a chunk")

    assert [seq for seq, _ in recording.stored_chunks(session_id)] == [0]


# --- Concatenation (spec §10.1) ---------------------------------------------


def test_concat_chunks_merges_in_sequence_order_regardless_of_upload_order(
    client, db, chunk_root
):
    """Acceptance criterion 2: uploading 2, 0, 1 still merges as 0, 1, 2.

    Out-of-order arrival is the normal case after a reconnect (ADR-0002 §8),
    so the merge must key off the client's sequence, never arrival order.
    """
    session_id = _new_session(db)
    payloads = {0: EBML_HEADER, 1: b"-middle-", 2: b"-tail-"}
    for sequence in (2, 0, 1):
        response = _post_chunk(client, session_id, sequence, payloads[sequence])
        assert response.status_code == 200, response.text

    merged = recording.concat_chunks(session_id)

    assert merged == recording.merged_path(session_id)
    assert merged.read_bytes() == payloads[0] + payloads[1] + payloads[2]
    # Sources are retained (ADR-0004 §4) and no temp file survives.
    assert _chunk_files(session_id) == ["000000.webm", "000001.webm", "000002.webm"]
    assert list(recording.session_dir(session_id).glob("*.part")) == []


def test_concat_chunks_refuses_a_take_with_no_header(chunk_root):
    """Acceptance criterion 2: a missing sequence 0 is an explicit error."""
    session_id = "S-headerless"
    _write_chunks(session_id, {1: b"body-one", 2: b"body-two"})

    with pytest.raises(recording.MissingHeaderChunk):
        recording.concat_chunks(session_id)

    assert not recording.merged_path(session_id).exists()


def test_concat_chunks_can_salvage_a_headerless_take_on_request(chunk_root):
    """The operator escape hatch still beats stranding the bytes."""
    session_id = "S-salvage"
    _write_chunks(session_id, {1: b"body-one", 2: b"body-two"})

    merged = recording.concat_chunks(session_id, allow_missing_header=True)

    assert merged.read_bytes() == b"body-onebody-two"


def test_concat_chunks_raises_when_nothing_was_uploaded(chunk_root):
    with pytest.raises(recording.NoChunksStored):
        recording.concat_chunks("S-empty")


def test_concat_chunks_merges_across_a_gap(chunk_root):
    """A lost slice costs its own seconds, not the whole recording."""
    session_id = "S-gap-merge"
    _write_chunks(session_id, {0: EBML_HEADER, 2: b"-after-the-gap"})

    merged = recording.concat_chunks(session_id)

    assert merged.read_bytes() == EBML_HEADER + b"-after-the-gap"


def test_concat_chunks_replaces_a_previous_merge_atomically(chunk_root):
    session_id = "S-remerge"
    _write_chunks(session_id, {0: EBML_HEADER})
    recording.concat_chunks(session_id)

    _write_chunks(session_id, {1: b"-appended"})
    merged = recording.concat_chunks(session_id)

    assert merged.read_bytes() == EBML_HEADER + b"-appended"
    assert list(recording.session_dir(session_id).glob("*.part")) == []


# --- Remux: command line + failure modes ------------------------------------


def test_remux_builds_the_h264_aac_faststart_command(chunk_root, monkeypatch):
    """Acceptance criterion 3: the codec/container flags spec §10.1 requires."""
    session_id = "S-cmdline"
    _write_chunks(session_id, {0: EBML_HEADER})
    recording.concat_chunks(session_id)

    seen: list[list[str]] = []

    def _capture(cmd, *args, **kwargs):
        seen.append(cmd)
        if Path(cmd[0]).name.startswith("ffprobe"):
            return subprocess.CompletedProcess(
                cmd, 0, b'{"streams":[{"codec_type":"video"}],"format":{}}', b""
            )
        # Stand in for FFmpeg by producing a non-empty output file.
        recording.mp4_path(session_id).write_bytes(b"fake-mp4-bytes")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _capture)

    out = recording.remux_to_mp4(session_id)

    assert out == recording.mp4_path(session_id)
    ffmpeg_cmd = seen[0]
    assert ffmpeg_cmd[0] == "ffmpeg"
    assert str(recording.merged_path(session_id)) in ffmpeg_cmd
    for flag, value in (
        ("-c:v", "libx264"),
        ("-profile:v", "baseline"),
        ("-pix_fmt", "yuv420p"),
        ("-c:a", "aac"),
        ("-movflags", "+faststart"),
    ):
        assert ffmpeg_cmd[ffmpeg_cmd.index(flag) + 1] == value
    assert ffmpeg_cmd[-1] == str(recording.mp4_path(session_id))


def test_remux_without_a_merged_stream_raises(chunk_root):
    with pytest.raises(recording.MergedStreamMissing):
        recording.remux_to_mp4("S-unmerged")


def test_missing_ffmpeg_raises_and_keeps_every_webm_artefact(chunk_root, monkeypatch):
    """ADR-0004 §4: no FFmpeg must never mean no recording."""
    session_id = "S-no-ffmpeg"
    _write_chunks(session_id, {0: EBML_HEADER, 1: b"-tail"})
    merged = recording.concat_chunks(session_id)
    merged_before = merged.read_bytes()

    monkeypatch.setattr(subprocess, "run", _missing_binary_run)

    with pytest.raises(recording.FFmpegUnavailable):
        recording.remux_to_mp4(session_id)

    assert merged.read_bytes() == merged_before
    assert _chunk_files(session_id) == ["000000.webm", "000001.webm"]


def test_failed_transcode_raises_and_keeps_every_webm_artefact(chunk_root, monkeypatch):
    """Acceptance criterion 3's second half: a broken stream loses nothing."""
    session_id = "S-broken"
    _write_chunks(session_id, {0: EBML_HEADER, 1: b"-tail"})
    merged = recording.concat_chunks(session_id)
    merged_before = merged.read_bytes()
    # A truncated output from a previous attempt, as FFmpeg would leave it.
    recording.mp4_path(session_id).write_bytes(b"truncated")

    monkeypatch.setattr(subprocess, "run", _failing_run(stderr=b"Invalid data found"))

    with pytest.raises(recording.RemuxFailed) as excinfo:
        recording.remux_to_mp4(session_id)

    assert "Invalid data found" in str(excinfo.value)
    assert merged.read_bytes() == merged_before
    assert _chunk_files(session_id) == ["000000.webm", "000001.webm"]
    # The invalid mp4 -- the lowest-priority artefact -- is the only casualty.
    assert not recording.mp4_path(session_id).exists()


def test_transcode_producing_no_output_is_reported_as_a_failure(chunk_root, monkeypatch):
    """A zero-exit FFmpeg that wrote nothing must not read as success."""
    session_id = "S-silent-failure"
    _write_chunks(session_id, {0: EBML_HEADER})
    recording.concat_chunks(session_id)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0, b"", b""),
    )

    with pytest.raises(recording.RemuxFailed):
        recording.remux_to_mp4(session_id)

    assert recording.merged_path(session_id).exists()


def test_probe_media_returns_none_without_ffprobe(chunk_root, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _missing_binary_run)
    assert recording.probe_media(Path("anything.mp4")) is None


# --- REST surface -----------------------------------------------------------


def test_status_endpoint_reports_completeness(client, db, chunk_root):
    session_id = _new_session(db)
    for sequence, data in ((0, EBML_HEADER), (1, b"-one"), (3, b"-three")):
        assert _post_chunk(client, session_id, sequence, data).status_code == 200

    body = client.get(
        f"/recording/{session_id}/status", headers=_student_headers(session_id)
    ).json()
    # Student status is the minimal upload view. The full inventory stays on
    # the library report the teacher route uses.
    assert set(body) == {
        "received_sequences",
        "next_expected_sequence",
        "final_sequence",
        "finalize_state",
        "recording_state",
    }
    assert body["received_sequences"] == [0, 1, 3]
    assert body["next_expected_sequence"] == 2
    assert body["finalize_state"] == "none"

    report = recording.get_recording_status(session_id, db)
    assert report["chunk_count"] == 3
    assert report["metadata_rows"] == 3
    assert report["first_sequence"] == 0
    assert report["last_sequence"] == 3
    assert report["missing_sequences"] == [2]
    assert report["header_chunk_ok"] is True
    assert report["total_bytes"] == len(EBML_HEADER) + len(b"-one") + len(b"-three")
    assert report["raw_webm"]["exists"] is False
    assert report["mp4"]["exists"] is False
    assert "chunks" not in report


def test_status_endpoint_can_include_per_chunk_metadata(client, db, chunk_root):
    session_id = _new_session(db)
    response = _post_chunk(client, session_id, 0, EBML_HEADER, start_ms=0)
    assert response.status_code == 200, response.text

    body = recording.get_recording_status(session_id, db, include_chunks=True)

    assert body["chunks"] == [
        {
            "sequence": 0,
            "size_bytes": len(EBML_HEADER),
            "checksum": recording.sha256_bytes(EBML_HEADER),
            "start_ms": 0,
            "end_ms": None,
            "upload_status": "received",
        }
    ]


def test_status_endpoint_is_safe_for_a_session_with_no_recording(client, db, chunk_root):
    session_id = _new_session(db)

    body = client.get(
        f"/recording/{session_id}/status", headers=_student_headers(session_id)
    ).json()

    assert body["received_sequences"] == []
    assert body["next_expected_sequence"] == 0
    assert body["final_sequence"] is None
    assert body["finalize_state"] == "none"
    assert body["recording_state"] == "recording"


def test_process_endpoint_degrades_to_raw_only_without_ffmpeg(
    client, db, chunk_root, monkeypatch
):
    """The route degrades where the library raises (ADR-0004 §4).

    A machine with no FFmpeg must still finish the session holding a complete
    merged WebM, and must say so plainly rather than returning a 500.
    """
    session_id = _new_session(db)
    for sequence, data in ((0, EBML_HEADER), (1, b"-tail")):
        assert _post_chunk(client, session_id, sequence, data).status_code == 200

    monkeypatch.setattr(subprocess, "run", _missing_binary_run)
    _mark_complete(db, session_id, final_sequence=1, total_chunks=2)

    response = client.post(f"/recording/{session_id}/process", headers=OPERATOR_HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "raw_only"
    assert body["chunk_count"] == 2
    assert body["raw_webm"]["exists"] is True
    assert body["raw_webm"]["path"] == f"{session_id}/student_raw.webm"
    assert body["mp4"] is None
    assert "FFmpeg binary not found" in body["remux_error"]
    assert recording.merged_path(session_id).read_bytes() == EBML_HEADER + b"-tail"


def test_process_endpoint_can_skip_the_transcode(client, db, chunk_root):
    session_id = _new_session(db)
    assert _post_chunk(client, session_id, 0, EBML_HEADER).status_code == 200
    _mark_complete(db, session_id, final_sequence=0, total_chunks=1)

    body = client.post(
        f"/recording/{session_id}/process",
        params={"remux": False},
        headers=OPERATOR_HEADERS,
    ).json()

    assert body["status"] == "raw_only"
    assert body["remux_error"] == "skipped"
    assert body["metadata_rows"] == 1
    assert not recording.mp4_path(session_id).exists()


def test_process_endpoint_409s_until_the_take_is_complete(client, db, chunk_root):
    """Transcode stays the same route, and it refuses an unfinished take."""
    session_id = _new_session(db)
    response = client.post(f"/recording/{session_id}/process", headers=OPERATOR_HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "recording_not_complete"
    assert not recording.merged_path(session_id).exists()


def test_process_endpoint_409s_without_a_header_chunk(client, db, chunk_root):
    session_id = _new_session(db)
    assert _post_chunk(client, session_id, 1, b"-orphan").status_code == 200
    rec = models.ensure_recording(db, session_id)
    assert rec is not None
    rec.recording_state = "complete"
    rec.finalize_state = "complete"
    rec.final_sequence = 1
    rec.total_chunks = 1
    db.commit()

    response = client.post(f"/recording/{session_id}/process", headers=OPERATOR_HEADERS)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "recording_not_complete"
    assert not recording.merged_path(session_id).exists()
    assert recording.chunk_path(session_id, 1).is_file()


# --- Real FFmpeg end to end (spec §10.1, M4 go/no-go) -----------------------


@needs_ffmpeg
def test_real_ffmpeg_pipeline_produces_h264_aac_within_the_duration_tolerance(
    client, db, chunk_root
):
    """The whole pipeline against the real binaries.

    A genuine 2s WebM is generated, split into byte slices the way
    MediaRecorder would, uploaded out of order, merged and transcoded. Byte
    splitting and re-concatenation is the identity operation, so the merged
    stream must equal the original -- which is what makes the duration
    comparison afterwards meaningful, and directly checks the M4 go/no-go
    ceiling of one second of drift.
    """
    session_id = _new_session(db)
    source = chunk_root / "source.webm"
    subprocess.run(
        [
            FFMPEG, "-y", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "80k",
            "-c:a", "libopus", "-b:a", "24k",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    original = source.read_bytes()
    assert original[: len(recording.EBML_MAGIC)] == recording.EBML_MAGIC

    # Slice it the way MediaRecorder would, then upload back to front.
    size = len(original) // 3 + 1
    slices = {i: original[i * size : (i + 1) * size] for i in range(3)}
    for sequence in (2, 1, 0):
        response = _post_chunk(client, session_id, sequence, slices[sequence])
        assert response.status_code == 200, response.text
    _mark_complete(db, session_id, final_sequence=2, total_chunks=3)

    body = client.post(
        f"/recording/{session_id}/process", headers=OPERATOR_HEADERS
    ).json()

    assert body["status"] == "completed", body.get("remux_error")
    assert body["missing_sequences"] == []
    # Concatenation is lossless: the merge is the original file, byte for byte.
    assert recording.merged_path(session_id).read_bytes() == original

    mp4 = recording.mp4_path(session_id)
    assert mp4.is_file() and mp4.stat().st_size > 0
    probe = recording.probe_media(mp4)
    assert probe is not None
    assert probe["has_video"] and probe["has_audio"]
    assert "h264" in probe["codecs"]
    assert "aac" in probe["codecs"]
    # M4 go/no-go: remux duration drift stays under one second.
    assert body["duration_delta_s"] is not None
    assert body["duration_delta_s"] < 1.0

    status = recording.get_recording_status(session_id, db)
    assert status["raw_webm"]["exists"] is True
    # The raw sources outlive a successful transcode (ADR-0004 §4).
    assert status["chunk_count"] == 3
    assert status["mp4"]["exists"] is True
