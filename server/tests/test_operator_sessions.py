"""Operator-only ``GET /sessions`` for the OP page (deploy design §17)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, recording
from app.main import SESSION_LIST_MAX, app
from app.models import Base

CASE_ID = "CASE001"
OP = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
NO_OP = {"X-No-Operator": "1"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def client(db_factory):
    def _override():
        session = db_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[models.get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.pop(models.get_db, None)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def chunk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path)
    return tmp_path


def _create(client: TestClient, session_id: str) -> dict:
    created = client.post(
        "/sessions", json={"session_id": session_id, "case_id": CASE_ID}, headers=OP
    )
    assert created.status_code == 201, created.text
    return created.json()


def _join(client: TestClient, session_id: str, code: str) -> str:
    joined = client.post(f"/sessions/{session_id}/join", json={"join_code": code})
    assert joined.status_code == 200, joined.text
    client.cookies.clear()
    return joined.json()["access_token"]


def _set_created_at(db, session_id: str, when: datetime) -> None:
    session = models.get_session(db, session_id)
    session.created_at = when
    db.commit()


def test_list_requires_operator_token(client):
    created = _create(client, "S-A")
    live = _join(client, "S-A", created["join_codes"]["wizard"])
    assert client.get("/sessions", headers=NO_OP).status_code == 401
    assert client.get("/sessions", headers=bearer("garbage")).status_code == 401
    # A valid live token is still not the operator.
    assert client.get("/sessions", headers=bearer(live)).status_code == 401


def test_list_is_newest_first_and_never_returns_codes(client, db):
    base = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    for offset, session_id in enumerate(("S-OLD", "S-MID", "S-NEW")):
        _create(client, session_id)
        _set_created_at(db, session_id, base + timedelta(minutes=offset))

    resp = client.get("/sessions", headers=OP)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["session_id"] for row in body["sessions"]] == ["S-NEW", "S-MID", "S-OLD"]
    assert body["upload_grace_seconds"] > 0
    row = body["sessions"][0]
    assert "join_codes" not in row and "review_codes" not in row
    assert row["status"] == "created"
    assert row["recording_state"] is None
    assert row["stored_sequences"] == 0
    assert row["seconds_since_last_chunk"] is None
    assert row["upload_grace_ends_at"] is None
    assert row["upload_reissue_refusal"] is None
    assert row["needs_operator"] is False


def test_limit_is_bounded(client, db):
    base = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    for offset in range(3):
        _create(client, f"S-{offset}")
        _set_created_at(db, f"S-{offset}", base + timedelta(minutes=offset))
    resp = client.get("/sessions", params={"limit": 2}, headers=OP)
    assert [row["session_id"] for row in resp.json()["sessions"]] == ["S-2", "S-1"]
    assert client.get("/sessions", params={"limit": 0}, headers=OP).status_code == 422
    over = client.get("/sessions", params={"limit": SESSION_LIST_MAX + 1}, headers=OP)
    assert over.status_code == 422


def test_ended_row_reports_grace_deadline_and_refusal(client, db, monkeypatch):
    monkeypatch.setenv("MINDPROBE_UPLOAD_GRACE_SECONDS", "600")
    _create(client, "S-E")
    assert client.post("/sessions/S-E/end", headers=OP).status_code == 200

    row = client.get("/sessions", headers=OP).json()["sessions"][0]
    assert row["status"] == "ended"
    ended_at = datetime.fromisoformat(row["ended_at"]).replace(tzinfo=timezone.utc)
    grace_end = datetime.fromisoformat(row["upload_grace_ends_at"])
    assert grace_end - ended_at == timedelta(seconds=600)
    assert row["upload_reissue_refusal"] is None

    session = models.get_session(db, "S-E")
    session.ended_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    db.commit()
    row = client.get("/sessions", headers=OP).json()["sessions"][0]
    assert row["upload_reissue_refusal"] == "upload_grace_expired"


def test_row_carries_recording_facts(client, db, chunk_root):
    _create(client, "S-R")
    chunks = chunk_root / "S-R" / "chunks"
    chunks.mkdir(parents=True)
    for sequence in range(3):
        (chunks / f"{sequence:06d}{recording.CHUNK_SUFFIX}").write_bytes(b"x")
    db.add(
        models.Recording(
            session_id="S-R",
            recording_state="incomplete",
            finalize_state="incomplete",
            incomplete_reason="recorder_lost",
            needs_operator=True,
        )
    )
    db.commit()

    row = client.get("/sessions", headers=OP).json()["sessions"][0]
    assert row["recording_state"] == "incomplete"
    assert row["finalize_state"] == "incomplete"
    assert row["incomplete_reason"] == "recorder_lost"
    assert row["needs_operator"] is True
    assert row["stored_sequences"] == 3
    assert isinstance(row["seconds_since_last_chunk"], int)
