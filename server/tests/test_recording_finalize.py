"""Recording completeness (ADR-0005 §7, design tests 11–13).

Finalize is idempotent, gaps stay un-complete until the grace check marks
them incomplete, and ``recorder_lost`` / operator incomplete never flip back
to complete — including after every chunk has arrived and after the scheduled
grace pass. The real FFmpeg pipeline stays in ``test_recording.py``.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("MINDPROBE_WS_TOKEN_SECRET", "test-finalize-secret")

from app import auth, models, recording, recording_integrity, websocket
from app.main import app
from app.models import Base

EBML_HEADER = recording.EBML_MAGIC + b"\x01\x00\x00\x00\x00\x00\x00\x23header"
OPERATOR = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
STUDENT_STATUS_KEYS = {
    "received_sequences",
    "next_expected_sequence",
    "final_sequence",
    "finalize_state",
    "recording_state",
}


@pytest.fixture()
def db_factory():
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
def chunk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path)
    return tmp_path


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _open_session(client: TestClient, session_id: str | None = None) -> tuple[str, dict[str, str]]:
    """Create a session and return role → access token."""
    session_id = session_id or f"S-{uuid4().hex[:8]}"
    created = client.post(
        "/sessions",
        json={"session_id": session_id, "case_id": "CASE001"},
        headers=OPERATOR,
    )
    assert created.status_code == 201, created.text
    tokens: dict[str, str] = {}
    for role, code in created.json()["join_codes"].items():
        joined = client.post(
            f"/sessions/{session_id}/join",
            json={"join_code": code, "client_id": f"{role}-test"},
        )
        assert joined.status_code == 200, joined.text
        tokens[role] = joined.json()["access_token"]
        client.cookies.clear()
    return session_id, tokens


def _upload(client, session_id: str, token: str, sequence: int, data: bytes):
    return client.post(
        f"/recording/{session_id}/chunk",
        data={"sequence": sequence},
        files={"chunk": (f"{sequence:06d}.webm", data, "video/webm")},
        headers=_bearer(token),
    )


def _finalize(client, session_id: str, token: str, **payload):
    body = {
        "final_sequence": 0,
        "total_chunks": 1,
        "total_duration_ms": 2000,
        **payload,
    }
    return client.post(
        f"/recording/{session_id}/finalize",
        json=body,
        headers=_bearer(token),
    )


def _chunk_names(session_id: str) -> list[str]:
    directory = recording.chunk_dir(session_id)
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("*.webm"))


def test_missing_for_final_counts_from_zero():
    """The old gap helper ignores a missing tail and a missing header."""
    assert recording.missing_sequences  # existing helper stays for concat
    assert recording_integrity.missing_for_final([0, 1], 2) == [2]
    assert recording_integrity.missing_for_final([1, 2], 2) == [0]
    assert recording_integrity.missing_for_final([0, 2], 2) == [1]
    assert recording_integrity.missing_for_final([0, 1, 2], 2) == []
    assert recording_integrity.missing_for_final([], -1) == []


def test_student_upload_status_is_minimal_and_grace_scoped(client, db, chunk_root):
    """Design test 11."""
    session_id, tokens = _open_session(client)
    other_id, other_tokens = _open_session(client)
    assert _upload(client, session_id, tokens["student"], 0, EBML_HEADER).status_code == 200

    own = client.get(
        f"/recording/{session_id}/status", headers=_bearer(tokens["student"])
    )
    assert own.status_code == 200, own.text
    body = own.json()
    assert set(body) == STUDENT_STATUS_KEYS
    assert body["received_sequences"] == [0]
    assert body["next_expected_sequence"] == 1
    assert body["final_sequence"] is None
    assert body["finalize_state"] == "none"
    assert body["recording_state"] == "recording"
    forbidden_words = ("path", "checksum", "size", "mp4", "webm", "chunk_count")
    assert all(word not in key for key in body for word in forbidden_words)

    other = client.get(
        f"/recording/{other_id}/status", headers=_bearer(tokens["student"])
    )
    assert other.status_code == 401
    assert other.json()["detail"] == "session_token_mismatch"

    for role in ("wizard", "teacher"):
        refused = client.get(
            f"/recording/{session_id}/status", headers=_bearer(tokens[role])
        )
        assert refused.status_code == 403, role

    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    during = client.get(
        f"/recording/{session_id}/status", headers=_bearer(tokens["student"])
    )
    assert during.status_code == 200, during.text
    # Grace does not reopen the rest of the API.
    closed = client.get(f"/sessions/{session_id}", headers=_bearer(tokens["student"]))
    assert closed.status_code == 401

    session = models.get_session(db, session_id)
    assert session is not None and session.ended_at is not None
    ended_at = session.ended_at
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=auth.utcnow().tzinfo)

    def _after_grace() -> object:
        return ended_at + timedelta(seconds=auth.upload_grace_seconds())

    # utcnow is patched only for the comparison. Monkeypatch replaces the
    # function auth.within_upload_grace calls.
    auth_utcnow = auth.utcnow
    monkey_deadline = ended_at + timedelta(seconds=auth.upload_grace_seconds())
    auth.utcnow = lambda: monkey_deadline  # type: ignore[method-assign]
    try:
        expired = client.get(
            f"/recording/{session_id}/status", headers=_bearer(tokens["student"])
        )
    finally:
        auth.utcnow = auth_utcnow  # type: ignore[method-assign]
    assert expired.status_code == 401
    assert expired.json()["detail"] == "revoked_session_token"
    assert _after_grace() == monkey_deadline

    # A leaked token stays dead inside the window too.
    session_id, tokens = _open_session(client)
    models.revoke_token_hash(
        db, session_id, models.hash_token(tokens["student"]), reason="security"
    )
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    for path, method in (
        (f"/recording/{session_id}/status", client.get),
        (f"/recording/{session_id}/finalize", client.post),
    ):
        kwargs = {"headers": _bearer(tokens["student"])}
        if method is client.post:
            kwargs["json"] = {
                "final_sequence": 0,
                "total_chunks": 1,
                "total_duration_ms": 1,
            }
        refused = method(path, **kwargs)
        assert refused.status_code == 401, path
    leaked_chunk = _upload(client, session_id, tokens["student"], 0, EBML_HEADER)
    assert leaked_chunk.status_code == 401


def test_finalize_is_idempotent_and_a_late_chunk_completes(client, db, chunk_root, monkeypatch):
    """Design test 12. Completeness does not itself run FFmpeg."""
    calls: list[object] = []

    def _spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("transcode must not start from finalize or chunk ingest")

    monkeypatch.setattr(recording, "process_recording", _spy)
    session_id, tokens = _open_session(client)
    token = tokens["student"]
    assert _upload(client, session_id, token, 0, EBML_HEADER).status_code == 200

    first = _finalize(
        client, session_id, token, final_sequence=2, total_chunks=3, total_duration_ms=6000
    )
    assert first.status_code == 200, first.text
    assert first.json()["finalize_state"] == "pending"
    assert first.json()["recording_state"] == "finalizing"
    assert first.json()["missing_sequences"] == [1, 2]

    second = _finalize(
        client, session_id, token, final_sequence=2, total_chunks=3, total_duration_ms=6000
    )
    assert second.status_code == 200, second.text
    assert second.json()["finalize_state"] == first.json()["finalize_state"]
    assert second.json()["missing_sequences"] == first.json()["missing_sequences"]
    assert second.json()["recording_state"] == first.json()["recording_state"]

    conflict = _finalize(
        client, session_id, token, final_sequence=2, total_chunks=3, total_duration_ms=1
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "finalize_conflict"

    # Still finalizing: a longer declaration (one more flushed slice) is allowed.
    grown = _finalize(
        client, session_id, token, final_sequence=3, total_chunks=4, total_duration_ms=8000
    )
    assert grown.status_code == 200, grown.text
    assert grown.json()["missing_sequences"] == [1, 2, 3]

    past = _upload(client, session_id, token, 4, b"too-far")
    assert past.status_code == 409
    assert past.json()["detail"]["error"] == "sequence_past_final"
    assert not recording.chunk_path(session_id, 4).exists()

    assert _upload(client, session_id, token, 1, b"-one").status_code == 200
    assert _upload(client, session_id, token, 2, b"-two").status_code == 200
    mid = client.get(f"/recording/{session_id}/status", headers=_bearer(token)).json()
    assert mid["finalize_state"] == "pending"
    assert mid["recording_state"] == "finalizing"

    assert _upload(client, session_id, token, 3, b"-three").status_code == 200
    done = client.get(f"/recording/{session_id}/status", headers=_bearer(token)).json()
    assert done["finalize_state"] == "complete"
    assert done["recording_state"] == "complete"
    assert done["next_expected_sequence"] is None
    assert calls == []
    assert _chunk_names(session_id) == [
        "000000.webm",
        "000001.webm",
        "000002.webm",
        "000003.webm",
    ]


def test_gaps_stay_finalizing_then_the_grace_check_marks_them_incomplete(
    client, db, chunk_root, monkeypatch
):
    """Tail, head, middle, total_chunks, and a repeated sequence do not complete."""
    monkeypatch.setattr(
        recording,
        "process_recording",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no transcode")),
    )
    session_id, tokens = _open_session(client)
    token = tokens["student"]

    # Missing tail.
    assert _upload(client, session_id, token, 0, EBML_HEADER).status_code == 200
    assert _upload(client, session_id, token, 1, b"-one").status_code == 200
    tail = _finalize(
        client, session_id, token, final_sequence=2, total_chunks=3, total_duration_ms=4000
    )
    assert tail.status_code == 200, tail.text
    assert tail.json()["recording_state"] == "finalizing"
    assert tail.json()["missing_sequences"] == [2]

    # total_chunks must equal final_sequence + 1. A mismatch is rejected and
    # does not replace the declaration already stored.
    mismatch = _finalize(
        client,
        session_id,
        token,
        final_sequence=3,
        total_chunks=9,
        total_duration_ms=4000,
    )
    assert mismatch.status_code == 422, mismatch.text
    db.expire_all()
    held = models.get_recording(db, session_id)
    assert held is not None
    assert held.final_sequence == 2
    assert held.total_chunks == 3
    assert held.recording_state == "finalizing"

    # Repeated upload of sequence 0 does not count as a second chunk.
    duplicate = _upload(client, session_id, token, 0, EBML_HEADER)
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert _chunk_names(session_id) == ["000000.webm", "000001.webm"]

    fresh_id, fresh_tokens = _open_session(client)
    fresh = fresh_tokens["student"]
    assert _upload(client, fresh_id, fresh, 1, b"-no-header").status_code == 200
    assert _upload(client, fresh_id, fresh, 2, b"-tail").status_code == 200
    head = _finalize(
        client, fresh_id, fresh, final_sequence=2, total_chunks=3, total_duration_ms=3000
    )
    assert head.json()["missing_sequences"] == [0]
    assert head.json()["recording_state"] == "finalizing"

    middle_id, middle_tokens = _open_session(client)
    middle = middle_tokens["student"]
    assert _upload(client, middle_id, middle, 0, EBML_HEADER).status_code == 200
    assert _upload(client, middle_id, middle, 2, b"-skip").status_code == 200
    gap = _finalize(
        client, middle_id, middle, final_sequence=2, total_chunks=3, total_duration_ms=3000
    )
    assert gap.json()["missing_sequences"] == [1]
    assert gap.json()["finalize_state"] == "pending"

    names_before = _chunk_names(session_id)
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    db.expire_all()
    changed = recording_integrity.apply_upload_grace_deadlines(
        db, now=auth.utcnow() + timedelta(hours=1)
    )
    assert session_id in changed
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.recording_state == "incomplete"
    assert rec.finalize_state == "incomplete"
    assert rec.incomplete_reason == "missing_chunks"
    assert _chunk_names(session_id) == names_before
    assert recording.chunk_path(session_id, 0).read_bytes() == EBML_HEADER
    refused = client.post(
        f"/recording/{session_id}/process", headers=OPERATOR
    )
    assert refused.status_code == 409
    assert not recording.merged_path(session_id).exists()

    # Filling the gap after the grace mark must not silently become complete.
    assert _upload(client, session_id, token, 2, b"-filled").status_code == 200
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.recording_state == "incomplete"
    assert rec.incomplete_reason == "missing_chunks"
    replay = _finalize(
        client, session_id, token, final_sequence=2, total_chunks=3, total_duration_ms=4000
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["recording_state"] == "incomplete"
    assert replay.json()["finalize_state"] == "incomplete"


def test_recorder_lost_and_operator_incomplete_never_become_complete(
    client, db, chunk_root, monkeypatch
):
    """Design test 13 / D8. Full chunks and the grace pass both leave it incomplete."""
    monkeypatch.setattr(
        recording,
        "process_recording",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no transcode")),
    )
    session_id, tokens = _open_session(client)
    token = tokens["student"]
    models.start_session(db, session_id)

    declared = _finalize(
        client,
        session_id,
        token,
        final_sequence=1,
        total_chunks=2,
        total_duration_ms=4000,
        reason="recorder_lost",
    )
    assert declared.status_code == 200, declared.text
    assert declared.json()["recording_state"] == "incomplete"
    assert declared.json()["finalize_state"] == "incomplete"

    again = _finalize(
        client,
        session_id,
        token,
        final_sequence=1,
        total_chunks=2,
        total_duration_ms=4000,
        reason="recorder_lost",
    )
    assert again.status_code == 200
    assert again.json() == declared.json()

    assert _upload(client, session_id, token, 0, EBML_HEADER).status_code == 200
    assert _upload(client, session_id, token, 1, b"-rest").status_code == 200
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.recording_state == "incomplete"
    assert rec.incomplete_reason == "recorder_lost"
    assert rec.needs_operator is True
    assert rec.missing_sequences == []
    assert _chunk_names(session_id) == ["000000.webm", "000001.webm"]

    session = models.get_session(db, session_id)
    assert session is not None
    assert session.status == "paused"
    assert session.pause_reason == "recording_failure"

    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    recording_integrity.apply_upload_grace_deadlines(
        db, now=auth.utcnow() + timedelta(hours=1)
    )
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.recording_state == "incomplete"
    assert rec.incomplete_reason == "recorder_lost"
    assert _chunk_names(session_id) == ["000000.webm", "000001.webm"]
    assert not recording.mp4_path(session_id).exists()
    assert not recording.merged_path(session_id).exists()
    refused = client.post(f"/recording/{session_id}/process", headers=OPERATOR)
    assert refused.status_code == 409

    other_id, other_tokens = _open_session(client)
    recording_integrity.mark_operator_incomplete(db, other_id)
    other = other_tokens["student"]
    assert _upload(client, other_id, other, 0, EBML_HEADER).status_code == 200
    filled = _finalize(
        client, other_id, other, final_sequence=0, total_chunks=1, total_duration_ms=2000
    )
    assert filled.status_code == 200, filled.text
    assert filled.json()["recording_state"] == "incomplete"
    assert filled.json()["finalize_state"] == "incomplete"
    assert client.post(f"/sessions/{other_id}/end", headers=OPERATOR).status_code == 200
    recording_integrity.apply_upload_grace_deadlines(
        db, now=auth.utcnow() + timedelta(hours=1)
    )
    db.expire_all()
    marked = models.get_recording(db, other_id)
    assert marked is not None
    assert marked.recording_state == "incomplete"
    assert marked.incomplete_reason == "operator_incomplete"
    assert recording.chunk_path(other_id, 0).is_file()


def test_finalize_during_grace_accepts_a_late_chunk(client, db, chunk_root):
    """Inside the window the three upload routes work; the chunk can still complete."""
    session_id, tokens = _open_session(client)
    token = tokens["student"]
    assert _upload(client, session_id, token, 0, EBML_HEADER).status_code == 200
    assert (
        _finalize(
            client, session_id, token, final_sequence=1, total_chunks=2, total_duration_ms=2000
        ).status_code
        == 200
    )
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200

    late = _upload(client, session_id, token, 1, b"-late")
    assert late.status_code == 200, late.text
    status = client.get(
        f"/recording/{session_id}/status", headers=_bearer(token)
    )
    assert status.status_code == 200, status.text
    assert status.json()["recording_state"] == "complete"
    assert status.json()["finalize_state"] == "complete"
    retried = _finalize(
        client, session_id, token, final_sequence=1, total_chunks=2, total_duration_ms=2000
    )
    assert retried.status_code == 200
    assert retried.json()["finalize_state"] == "complete"


def test_finalize_rejects_a_final_sequence_below_stored_chunks(client, db, chunk_root):
    session_id, tokens = _open_session(client)
    token = tokens["student"]
    assert _upload(client, session_id, token, 0, EBML_HEADER).status_code == 200
    assert _upload(client, session_id, token, 1, b"-one").status_code == 200
    assert _upload(client, session_id, token, 2, b"-two").status_code == 200

    response = _finalize(
        client, session_id, token, final_sequence=1, total_chunks=2, total_duration_ms=3000
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "final_sequence_below_stored"
    assert detail["stored_above"] == [2]
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.final_sequence is None
    assert rec.recording_state == "recording"


def test_zero_chunk_session_is_incomplete_after_grace_and_visible_to_the_teacher(
    client, db, chunk_root
):
    session_id, tokens = _open_session(client)
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    db.expire_all()
    opened = models.get_recording(db, session_id)
    assert opened is not None
    assert opened.recording_state == "finalizing"

    before = client.get(
        f"/sessions/{session_id}/recording/status",
        headers=_bearer(tokens["teacher"]),
    )
    assert before.status_code == 200, before.text
    assert before.json()["chunk_count"] == 0
    assert before.json()["recording_state"] == "finalizing"

    changed = recording_integrity.apply_upload_grace_deadlines(
        db, now=auth.utcnow() + timedelta(hours=1)
    )
    assert session_id in changed
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.recording_state == "incomplete"
    assert rec.incomplete_reason == "grace_expired"
    audits = db.scalars(
        select(models.AuditLog).where(
            models.AuditLog.session_id == session_id,
            models.AuditLog.action == recording_integrity.AUDIT_RECORDING_INCOMPLETE,
        )
    ).all()
    assert audits
    assert audits[-1].details["reason"] == "grace_expired"

    after = client.get(
        f"/sessions/{session_id}/recording/status",
        headers=_bearer(tokens["teacher"]),
    )
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["chunk_count"] == 0
    assert body["recording_state"] == "incomplete"
    assert body["incomplete_reason"] == "grace_expired"


def test_grace_creates_incomplete_when_the_recording_row_is_missing(db):
    session_id = f"S-{uuid4().hex[:8]}"
    models.create_session(db, session_id, "CASE001")
    models.end_session(db, session_id)
    assert models.get_recording(db, session_id) is None
    changed = recording_integrity.apply_upload_grace_deadlines(
        db, now=auth.utcnow() + timedelta(hours=1)
    )
    assert session_id in changed
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.recording_state == "incomplete"
    assert rec.finalize_state == "incomplete"
    assert rec.incomplete_reason == "grace_expired"


def test_grace_claim_does_not_overwrite_an_incomplete_reason(db):
    session_id = f"S-{uuid4().hex[:8]}"
    models.create_session(db, session_id, "CASE001")
    models.end_session(db, session_id)
    rec = models.ensure_recording(db, session_id)
    assert rec is not None
    rec.recording_state = "incomplete"
    rec.finalize_state = "incomplete"
    rec.incomplete_reason = "grace_expired"
    rec.needs_operator = False
    db.commit()

    claimed = recording_integrity.claim_open_recording(
        db,
        session_id,
        {
            "recording_state": "complete",
            "finalize_state": "complete",
            "incomplete_reason": None,
            "missing_sequences": [],
        },
    )
    assert claimed is False
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.recording_state == "incomplete"
    assert rec.incomplete_reason == "grace_expired"

    # needs_operator latches even while the state still says finalizing.
    rec.recording_state = "finalizing"
    rec.needs_operator = True
    rec.incomplete_reason = "recorder_lost"
    db.commit()
    changed = recording_integrity.apply_upload_grace_deadlines(
        db, now=auth.utcnow() + timedelta(hours=1)
    )
    assert session_id not in changed
    db.expire_all()
    rec = models.get_recording(db, session_id)
    assert rec is not None
    assert rec.incomplete_reason == "recorder_lost"
    assert rec.needs_operator is True
