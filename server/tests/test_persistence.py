"""Persistence + join-token tests (spec 9; ADR-0001 7/8; ADR-0004 1-4).

Covers the Server/Persistence Agent slice: the session state machine, event
idempotency, the REST surface, and the audit log no-raw-tokens rule.

Every test runs against its own in-memory SQLite engine, injected through the
``get_db`` FastAPI dependency, so nothing here touches the real
``sessions/mind-probe.sqlite`` and no state leaks between tests.
"""

from __future__ import annotations

import logging
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

# Must be set before app.main imports app.websocket, which reads it when
# signing. setdefault so we never clobber a secret an earlier test module set.
os.environ.setdefault("MINDPROBE_WS_TOKEN_SECRET", "test-persistence-secret")

from app import models, websocket
from app.events import EventEnvelope, EventType, Source
from app.main import app
from app.models import Base


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def db_factory():
    """A sessionmaker bound to a private in-memory database.

    StaticPool keeps every session on the same connection, so the ``:memory:``
    database is shared between the caller session and the one the request
    handler opens (TestClient runs handlers on a different thread).
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
    """TestClient with get_db pointed at the test database.

    Deliberately *not* used as a context manager: entering it would run the
    lifespan (and therefore ``init_db()``) against the real engine. The
    lifespan is exercised on its own in ``test_lifespan_*`` below.
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


def _envelope(session_id: str, sequence: int, **payload) -> EventEnvelope:
    return EventEnvelope(
        session_id=session_id,
        sequence=sequence,
        client_timestamp_ms=1000 + sequence,
        source=Source.WIZARD,
        event_type=EventType.CLIP_COMMAND,
        payload=payload,
    )


class _FakeWebSocket:
    """Minimal stand-in exposing the only attribute ``_cookie_value`` reads."""

    def __init__(self, cookie_header: str) -> None:
        self.headers = {"cookie": cookie_header}


def _create_session_and_join_codes(
    client: TestClient, session_id: str = "S001", case_id: str = "CASE001"
) -> dict[str, str]:
    resp = client.post("/sessions", json={"session_id": session_id, "case_id": case_id})
    assert resp.status_code == 201
    return resp.json()["join_codes"]


# --- Session lifecycle (ADR-0004 1) -----------------------------------------


def test_create_session_starts_at_created_revision_zero(db):
    session = models.create_session(db, "S001", "CASE001")
    assert session.status == "created"
    assert session.snapshot_revision == 0
    assert session.pause_reason is None
    assert models.get_session(db, "S001") is session


def test_get_session_returns_none_when_absent(db):
    assert models.get_session(db, "S404") is None


def test_full_lifecycle_bumps_revision_on_every_transition(db):
    models.create_session(db, "S001", "CASE001")

    scheduled = models.start_session(db, "S001", start_at_ms=5000)
    assert (scheduled.status, scheduled.snapshot_revision) == ("starting", 1)
    assert scheduled.start_at_ms == 5000

    active = models.start_session(db, "S001")
    assert (active.status, active.snapshot_revision) == ("active", 2)

    paused = models.pause_session(db, "S001", "network_failure")
    assert (paused.status, paused.snapshot_revision) == ("paused", 3)
    assert paused.pause_reason == "network_failure"

    resumed = models.resume_session(db, "S001")
    assert (resumed.status, resumed.snapshot_revision) == ("active", 4)
    assert resumed.pause_reason is None

    ended = models.end_session(db, "S001")
    assert (ended.status, ended.snapshot_revision) == ("ended", 5)
    assert ended.ended_at is not None


def test_start_session_cannot_resume_paused_session(db):
    models.create_session(db, "S001", "CASE001")
    models.start_session(db, "S001")
    models.pause_session(db, "S001", "manual")

    with pytest.raises(ValueError, match="use resume_session"):
        models.start_session(db, "S001")

    session = models.get_session(db, "S001")
    assert (session.status, session.snapshot_revision) == ("paused", 2)
    assert session.pause_reason == "manual"


def test_five_states_match_websocket_snapshot_default():
    """The DB state machine must speak the same vocabulary as SessionSnapshot."""
    assert set(models.SESSION_STATES) == {
        "created",
        "starting",
        "active",
        "paused",
        "ended",
    }
    assert websocket.SessionSnapshot().session_state in models.SESSION_STATES


@pytest.mark.parametrize("reason", sorted(models.PAUSE_REASONS))
def test_every_adr_pause_reason_is_accepted(db, reason):
    models.create_session(db, "S001", "CASE001")
    models.start_session(db, "S001")
    assert models.pause_session(db, "S001", reason).pause_reason == reason


@pytest.mark.parametrize("reason", ["", "MANUAL", "operator_bored", "timeout", None])
def test_invalid_pause_reason_is_rejected(db, reason):
    models.create_session(db, "S001", "CASE001")
    models.start_session(db, "S001")
    with pytest.raises(ValueError):
        models.pause_session(db, "S001", reason)
    # The rejected pause must not move the session or bump the revision.
    session = models.get_session(db, "S001")
    assert (session.status, session.snapshot_revision) == ("active", 1)


def test_pause_reasons_match_websocket_module():
    """ADR-0004 2 enum is duplicated in two modules -- pin them together."""
    assert set(models.PAUSE_REASONS) == set(websocket.PAUSE_REASONS)


def test_illegal_transitions_are_rejected(db):
    models.create_session(db, "S001", "CASE001")
    # Cannot resume something that was never paused.
    with pytest.raises(ValueError):
        models.resume_session(db, "S001")
    # Cannot pause a session that has not started.
    with pytest.raises(ValueError):
        models.pause_session(db, "S001", "manual")
    # ended is terminal.
    models.end_session(db, "S001")
    with pytest.raises(ValueError):
        models.start_session(db, "S001")


def test_end_session_is_idempotent(db):
    models.create_session(db, "S001", "CASE001")
    first = models.end_session(db, "S001")
    revision, ended_at = first.snapshot_revision, first.ended_at
    second = models.end_session(db, "S001")
    assert second.snapshot_revision == revision
    assert second.ended_at == ended_at


def test_revoke_session_tokens_records_one_revocation_per_issued_token_hash(db):
    for token in ("header.signature-one", "header.signature-two"):
        models.record_audit(
            db,
            action=models.AUDIT_TOKEN_ISSUED,
            actor_role="student",
            session_id="S001",
            token=token,
        )

    entries = models.revoke_session_tokens(db, "S001")
    assert [entry.action for entry in entries] == [
        models.AUDIT_TOKEN_REVOKED,
        models.AUDIT_TOKEN_REVOKED,
    ]
    assert {entry.token_hash for entry in entries} == {
        models.hash_token("header.signature-one"),
        models.hash_token("header.signature-two"),
    }

    # Idempotent: a second pass does not duplicate revocation rows.
    assert models.revoke_session_tokens(db, "S001") == []


def test_issue_join_codes_stores_only_hashes_and_resolves_role(db):
    models.create_session(db, "S001", "CASE001")
    codes = models.issue_join_codes(db, "S001")

    assert set(codes) == set(models.JOIN_CODE_ROLES)
    rows = db.scalars(select(models.JoinCode)).all()
    assert len(rows) == len(models.JOIN_CODE_ROLES)
    for row in rows:
        assert row.code_hash == models.hash_join_code(codes[row.role])
        assert codes[row.role] not in row.code_hash
        assert row.consumed_at is None

    consumed = models.consume_join_code(db, "S001", codes["wizard"])
    assert consumed.role == "wizard"
    assert consumed.consumed_at is not None


def test_join_code_can_be_consumed_only_once(db):
    models.create_session(db, "S001", "CASE001")
    code = models.issue_join_codes(db, "S001")["student"]
    models.consume_join_code(db, "S001", code)
    with pytest.raises(models.JoinCodeConsumed):
        models.consume_join_code(db, "S001", code)


def test_join_code_is_bound_to_its_session(db):
    models.create_session(db, "S001", "CASE001")
    models.create_session(db, "S002", "CASE001")
    code = models.issue_join_codes(db, "S001")["teacher"]
    models.issue_join_codes(db, "S002")
    with pytest.raises(models.JoinCodeInvalid):
        models.consume_join_code(db, "S002", code)


def test_lifecycle_helpers_reject_unknown_session(db):
    for call in (
        lambda: models.start_session(db, "S404"),
        lambda: models.pause_session(db, "S404", "manual"),
        lambda: models.resume_session(db, "S404"),
        lambda: models.end_session(db, "S404"),
    ):
        with pytest.raises(models.SessionNotFound):
            call()


# --- Event persistence (ADR-0001 7, ADR-0004 4) -----------------------------


def _event_count(db) -> int:
    return db.scalar(select(func.count()).select_from(models.Event))


def test_dedup_key_matches_websocket_implementation():
    """models.event_dedup_key must be identical to websocket._event_dedup_key."""
    payloads = [
        {},
        {"event_id": "E1"},
        {"command_id": "C1"},
        {"event_id": "E1", "command_id": "C1"},
        {"event_id": "", "command_id": "C1"},  # falsy -> falls through
        {"event_id": None, "command_id": None},  # both falsy -> triple
        {"event_id": 0, "command_id": "C1"},
        {"event_id": 42},
    ]
    for payload in payloads:
        env = _envelope("S001", 7, **payload)
        assert models.event_dedup_key(env) == websocket._event_dedup_key(env), payload


def test_append_event_persists_envelope_fields(db):
    models.create_session(db, "S001", "CASE001")
    env = _envelope("S001", 1, event_id="E1", clip_id="C001_TIMELINE_03")
    env.server_timestamp_ms = 68455
    env.clock_offset_ms = 35

    event, is_new = models.append_event(db, "S001", env)
    assert is_new is True
    assert event.sequence == 1
    assert event.source == "wizard"
    assert event.event_type == "clip_command"
    assert event.client_timestamp_ms == 1001
    assert event.server_timestamp_ms == 68455
    assert event.clock_offset_ms == 35
    assert event.payload["clip_id"] == "C001_TIMELINE_03"
    assert event.dedup_key == "E1"


def test_append_event_is_idempotent_by_explicit_event_id(db):
    models.create_session(db, "S001", "CASE001")
    first, new_first = models.append_event(
        db, "S001", _envelope("S001", 1, event_id="E1")
    )
    # Same event_id resent after a reconnect, but with a different sequence.
    second, new_second = models.append_event(
        db, "S001", _envelope("S001", 9, event_id="E1")
    )
    assert new_first is True and new_second is False
    assert second.id == first.id
    assert _event_count(db) == 1


def test_append_event_is_idempotent_by_session_source_sequence(db):
    """Same (session_id, source, sequence) twice -> exactly one row.

    Even when the two envelopes carry *different* explicit event ids, the
    ADR-0001 7 fallback constraint still collapses them.
    """
    models.create_session(db, "S001", "CASE001")
    models.append_event(db, "S001", _envelope("S001", 4))
    _, is_new = models.append_event(db, "S001", _envelope("S001", 4))
    assert is_new is False
    _, is_new_other = models.append_event(
        db, "S001", _envelope("S001", 4, event_id="E9")
    )
    assert is_new_other is False
    assert _event_count(db) == 1


def test_same_sequence_from_different_source_is_a_distinct_event(db):
    models.create_session(db, "S001", "CASE001")
    models.append_event(db, "S001", _envelope("S001", 4))
    student = EventEnvelope(
        session_id="S001",
        sequence=4,
        client_timestamp_ms=1004,
        source=Source.STUDENT,
        event_type=EventType.CLIP_INTERRUPTED,
    )
    _, is_new = models.append_event(db, "S001", student)
    assert is_new is True
    assert _event_count(db) == 2


def test_duplicate_append_never_overwrites_the_stored_row(db):
    """ADR-0004 4: a persisted raw event is immutable."""
    models.create_session(db, "S001", "CASE001")
    models.append_event(
        db, "S001", _envelope("S001", 1, event_id="E1", note="original")
    )
    models.append_event(
        db, "S001", _envelope("S001", 1, event_id="E1", note="tampered")
    )
    db.expire_all()
    (stored,) = models.load_events(db, "S001")
    assert stored.payload["note"] == "original"


def test_load_events_returns_sequence_order(db):
    models.create_session(db, "S001", "CASE001")
    for sequence in (5, 1, 4, 2, 3):
        models.append_event(db, "S001", _envelope("S001", sequence))
    assert [e.sequence for e in models.load_events(db, "S001")] == [1, 2, 3, 4, 5]


def test_load_events_is_scoped_to_one_session(db):
    models.create_session(db, "S001", "CASE001")
    models.create_session(db, "S002", "CASE001")
    models.append_event(db, "S001", _envelope("S001", 1))
    models.append_event(db, "S002", _envelope("S002", 1))
    assert [e.session_id for e in models.load_events(db, "S001")] == ["S001"]


def test_append_event_rejects_unknown_session(db):
    with pytest.raises(models.SessionNotFound):
        models.append_event(db, "S404", _envelope("S404", 1))


def test_append_event_rejects_envelope_session_mismatch(db):
    models.create_session(db, "S001", "CASE001")
    with pytest.raises(ValueError):
        models.append_event(db, "S001", _envelope("S002", 1))


# --- Audit log (ADR-0004 3) -------------------------------------------------


def test_record_audit_stores_only_the_token_hash(db):
    token = "header.signature-that-must-never-be-stored"
    entry = models.record_audit(
        db,
        action=models.AUDIT_TOKEN_ISSUED,
        actor_role="student",
        session_id="S001",
        token=token,
    )
    assert entry.token_hash == models.hash_token(token)
    assert len(entry.token_hash) == 64
    assert token not in entry.token_hash


def test_record_audit_refuses_a_raw_token_smuggled_into_details(db):
    token = websocket.create_session_token("S001", "student")
    with pytest.raises(ValueError):
        models.record_audit(
            db,
            action=models.AUDIT_TOKEN_ISSUED,
            actor_role="student",
            token=token,
            details={"debug": f"issued {token}"},
        )


def test_record_audit_refuses_raw_token_in_nested_details(db):
    token = websocket.create_session_token("S001", "student")

    for details in (
        {"nested": {"token": token}},
        {"nested": ["safe", {"debug": f"issued {token}"}]},
        {token: "token in key"},
    ):
        with pytest.raises(ValueError):
            models.record_audit(
                db,
                action=models.AUDIT_TOKEN_ISSUED,
                actor_role="student",
                token=token,
                details=details,
            )

    assert db.scalars(select(models.AuditLog)).all() == []


def test_record_audit_refuses_a_token_shaped_detail_with_no_token_argument(db):
    """The guard must hold on the path callers actually use.

    Connection-takeover audits pass no ``token=``, so a token leaking through
    ``details`` there would otherwise be unscreened.
    """
    token = websocket.create_session_token("S001", "wizard")
    with pytest.raises(ValueError):
        models.record_audit(
            db,
            action=models.AUDIT_CONNECTION_TAKEOVER,
            actor_role="wizard",
            session_id="S001",
            details={"debug": f"replacing connection holding {token}"},
        )
    assert db.scalars(select(models.AuditLog)).all() == []


def test_record_audit_allows_ordinary_details(db):
    """The token-shape screen must not trip on normal metadata."""
    entry = models.record_audit(
        db,
        action=models.AUDIT_CONNECTION_TAKEOVER,
        actor_role="wizard",
        session_id="S001",
        details={
            "clip_id": "C001_TIMELINE_03",
            "reason": "heartbeat_stale",
            "previous_connected_at_ms": 1234567890,
        },
    )
    assert entry.details["clip_id"] == "C001_TIMELINE_03"
    assert entry.token_hash is None


def test_pause_is_audited_with_actor_and_reason(db):
    models.create_session(db, "S001", "CASE001")
    models.start_session(db, "S001")
    models.pause_session(db, "S001", "recording_failure", actor_role="wizard")

    entries = db.scalars(
        select(models.AuditLog).where(
            models.AuditLog.action == models.AUDIT_SESSION_PAUSED
        )
    ).all()
    assert len(entries) == 1
    assert entries[0].actor_role == "wizard"
    assert entries[0].session_id == "S001"
    assert entries[0].details["reason"] == "recording_failure"
    assert entries[0].token_hash is None


# --- Session REST surface ---------------------------------------------------


def test_create_get_and_end_session_over_rest(client):
    created = client.post(
        "/sessions", json={"session_id": "S001", "case_id": "CASE001"}
    )
    assert created.status_code == 201
    assert created.json()["status"] == "created"
    assert created.json()["snapshot_revision"] == 0
    assert set(created.json()["join_codes"]) == set(models.JOIN_CODE_ROLES)

    fetched = client.get("/sessions/S001")
    assert fetched.status_code == 200
    assert fetched.json()["case_id"] == "CASE001"
    assert "join_codes" not in fetched.json()

    ended = client.post("/sessions/S001/end")
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    assert ended.json()["snapshot_revision"] == 1
    assert ended.json()["ended_at"] is not None


def test_create_session_conflicts_on_duplicate_id(client):
    client.post("/sessions", json={"session_id": "S001", "case_id": "CASE001"})
    again = client.post("/sessions", json={"session_id": "S001", "case_id": "CASE001"})
    assert again.status_code == 409


def test_unknown_session_returns_404(client):
    assert client.get("/sessions/S404").status_code == 404
    assert client.get("/sessions/S404/events").status_code == 404
    assert client.post("/sessions/S404/end").status_code == 404
    join = client.post("/sessions/S404/join", json={"join_code": "missing"})
    assert join.status_code == 404


def test_get_events_returns_envelope_shaped_rows_in_order(client, db):
    client.post("/sessions", json={"session_id": "S001", "case_id": "CASE001"})
    for seq in (3, 1, 2):
        models.append_event(db, "S001", _envelope("S001", seq, event_id=f"E{seq}"))

    body = client.get("/sessions/S001/events").json()
    assert [e["sequence"] for e in body["events"]] == [1, 2, 3]
    assert set(body["events"][0]) == {
        "session_id",
        "sequence",
        "client_timestamp_ms",
        "server_timestamp_ms",
        "clock_offset_ms",
        "source",
        "event_type",
        "payload",
    }
    # The serialized row must re-validate as the shared envelope contract.
    EventEnvelope.model_validate(body["events"][0])


# --- Join -> token -> cookie (ADR-0001 8) -----------------------------------


def test_join_sets_httponly_role_bound_cookie(client):
    join_codes = _create_session_and_join_codes(client)
    resp = client.post(
        "/sessions/S001/join",
        json={"join_code": join_codes["student"], "client_id": "dev-1"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"session_id": "S001", "role": "student", "status": "ok"}
    # The token is delivered only as a cookie, never in the body.
    assert "token" not in resp.text

    # Starlette normalises the samesite value to lowercase, so compare
    # case-insensitively rather than pinning its formatting.
    set_cookie = resp.headers["set-cookie"].lower()
    assert websocket.WS_SESSION_COOKIE in set_cookie
    assert "httponly" in set_cookie
    assert "secure" not in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie
    assert resp.cookies.get(websocket.WS_SESSION_COOKIE)


def test_join_cookie_secure_attribute_is_enabled_outside_dev(
    client, monkeypatch
):
    monkeypatch.setenv("MINDPROBE_ENV", "production")
    join_codes = _create_session_and_join_codes(client)
    resp = client.post("/sessions/S001/join", json={"join_code": join_codes["student"]})
    assert resp.status_code == 200
    assert "secure" in resp.headers["set-cookie"].lower()


def test_join_cookie_is_accepted_by_verify_session_token(client):
    """End-to-end proof the issued token really is what the WS upgrade wants."""
    join_codes = _create_session_and_join_codes(client)
    resp = client.post("/sessions/S001/join", json={"join_code": join_codes["wizard"]})
    token = resp.cookies[websocket.WS_SESSION_COOKIE]

    fake_ws = _FakeWebSocket(f"{websocket.WS_SESSION_COOKIE}={token}")
    # Correct session + role: no exception.
    websocket._verify_session_token(fake_ws, "S001", "wizard")
    # The token is bound to both -- neither may be swapped.
    with pytest.raises(websocket.WebSocketAuthError):
        websocket._verify_session_token(fake_ws, "S001", "student")
    with pytest.raises(websocket.WebSocketAuthError):
        websocket._verify_session_token(fake_ws, "S999", "wizard")


def test_join_cookie_completes_a_real_websocket_upgrade(client):
    """The full M1 unblock: join -> cookie -> live /ws socket.

    Goes through the real upgrade rather than calling
    ``_verify_session_token`` on a stub, so the cookie the endpoint sets is
    proven acceptable to ``websocket.py`` exactly as a browser would present
    it.
    """
    join_codes = _create_session_and_join_codes(client)
    resp = client.post("/sessions/S001/join", json={"join_code": join_codes["wizard"]})
    token = resp.cookies[websocket.WS_SESSION_COOKIE]

    headers = {"cookie": f"{websocket.WS_SESSION_COOKIE}={token}"}
    with client.websocket_connect("/ws/S001/wizard", headers=headers) as sock:
        sock.send_json({"type": "ping", "ping_id": "p1", "client_timestamp_ms": 0})
        reply = sock.receive_json()
        assert reply["type"] == "pong"
        assert reply["ping_id"] == "p1"

    # The same token must not open a socket for the role it is not bound to.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/S001/student", headers=headers):
            pass


def test_join_rejects_client_supplied_role(client):
    join_codes = _create_session_and_join_codes(client)
    resp = client.post(
        "/sessions/S001/join",
        json={"join_code": join_codes["student"], "role": "wizard"},
    )
    assert resp.status_code == 422


def test_join_rejects_reused_code(client):
    join_codes = _create_session_and_join_codes(client)
    body = {"join_code": join_codes["student"]}
    assert client.post("/sessions/S001/join", json=body).status_code == 200
    assert client.post("/sessions/S001/join", json=body).status_code == 409


def test_join_rejects_code_from_another_session(client):
    first_codes = _create_session_and_join_codes(client, "S001")
    _create_session_and_join_codes(client, "S002")
    resp = client.post("/sessions/S002/join", json={"join_code": first_codes["teacher"]})
    assert resp.status_code == 403


def test_join_rejects_an_ended_session(client):
    join_codes = _create_session_and_join_codes(client)
    client.post("/sessions/S001/end")
    resp = client.post("/sessions/S001/join", json={"join_code": join_codes["student"]})
    assert resp.status_code == 409


def test_end_session_revokes_existing_cookie_and_blocks_websocket_upgrade(
    client, db
):
    join_codes = _create_session_and_join_codes(client)
    join = client.post("/sessions/S001/join", json={"join_code": join_codes["wizard"]})
    token = join.cookies[websocket.WS_SESSION_COOKIE]

    ended = client.post("/sessions/S001/end")
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"

    entries = db.scalars(
        select(models.AuditLog)
        .where(models.AuditLog.action == models.AUDIT_TOKEN_REVOKED)
        .where(models.AuditLog.session_id == "S001")
    ).all()
    assert len(entries) == 1
    assert entries[0].token_hash == models.hash_token(token)
    assert token not in str(entries[0].details)

    headers = {"cookie": f"{websocket.WS_SESSION_COOKIE}={token}"}
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/S001/wizard", headers=headers):
            pass
    assert exc_info.value.code == 4401


def test_join_audits_the_issued_token_without_storing_it(client, db):
    join_codes = _create_session_and_join_codes(client)
    raw_code = join_codes["student"]
    resp = client.post("/sessions/S001/join", json={"join_code": raw_code})
    token = resp.cookies[websocket.WS_SESSION_COOKIE]

    entries = db.scalars(select(models.AuditLog)).all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == models.AUDIT_TOKEN_ISSUED
    assert entry.actor_role == "student"
    assert entry.session_id == "S001"
    assert entry.token_hash == models.hash_token(token)

    # No column anywhere in audit_logs may contain the raw token, its
    # signature half, or the raw one-time join code (ADR-0004 3).
    signature = token.split(".", 1)[1]
    columns = (
        entry.session_id,
        entry.action,
        entry.actor_role,
        entry.token_hash,
        str(entry.details),
    )
    for value in (token, signature, raw_code):
        for column_value in columns:
            assert value not in (column_value or "")


def test_failed_token_signing_does_not_consume_join_code(
    client, db, monkeypatch
):
    join_codes = _create_session_and_join_codes(client)
    raw_code = join_codes["teacher"]

    def _fail_to_sign(*args, **kwargs):
        raise websocket.WebSocketAuthError("test signing failure")

    monkeypatch.setattr(websocket, "create_session_token", _fail_to_sign)
    resp = client.post("/sessions/S001/join", json={"join_code": raw_code})
    assert resp.status_code == 500

    row = db.scalars(
        select(models.JoinCode)
        .where(models.JoinCode.session_id == "S001")
        .where(models.JoinCode.code_hash == models.hash_join_code(raw_code))
    ).one()
    assert row.consumed_at is None
    assert db.scalars(select(models.AuditLog)).all() == []


# --- Dev token-secret fallback ----------------------------------------------


def test_lifespan_generates_a_dev_secret_without_logging_it(monkeypatch, caplog):
    """Unset secret -> app still boots, secret populated, never printed."""
    monkeypatch.delenv(websocket.WS_TOKEN_SECRET_ENV, raising=False)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(app):
            pass

    secret = os.environ.get(websocket.WS_TOKEN_SECRET_ENV)
    assert secret, "lifespan must populate the secret so tokens can be signed"
    assert len(secret) >= 32
    assert websocket.WS_TOKEN_SECRET_ENV in caplog.text
    assert secret not in caplog.text
    # The same secret is now visible to websocket.py, so a token signed by the
    # join endpoint verifies at the WebSocket upgrade.
    assert websocket._token_secret() == secret.encode("utf-8")


def test_lifespan_keeps_an_explicitly_configured_secret(monkeypatch):
    monkeypatch.setenv(websocket.WS_TOKEN_SECRET_ENV, "configured-secret")
    with TestClient(app):
        pass
    assert os.environ[websocket.WS_TOKEN_SECRET_ENV] == "configured-secret"
