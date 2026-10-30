"""Operator join-code reissue: ``POST /sessions/{id}/join-codes``.

Old codes die at once; live tokens survive unless ``revoke_live``. With it,
only the chosen roles' live tokens are revoked, for reason
``join_codes_reissued``, in the same transaction as the new codes and the
audit. Review grants, upload tokens and the upload grant are untouched.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app import auth, models, recording
from app.main import app
from app.models import Base
from app.websocket import WS_PROTOCOL

OPERATOR = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
EBML = recording.EBML_MAGIC + b"\x01\x00\x00\x00\x00\x00\x00\x23header"

client = TestClient(app)


@pytest.fixture(autouse=True)
def db_factory(monkeypatch):
    """One in-memory DB behind both ``get_db`` and ``session_scope``.

    Both resolve ``models.SessionLocal`` at call time, so REST joins and the
    WebSocket upgrade see the same tokens and revocations.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(models, "SessionLocal", factory)
    auth.reset_upload_rate_limits()
    yield factory
    auth.reset_upload_rate_limits()
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def chunk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path)
    return tmp_path


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create() -> tuple[str, dict]:
    session_id = f"S-{uuid4().hex[:10]}"
    created = client.post(
        "/sessions", json={"session_id": session_id, "case_id": "CASE001"}, headers=OPERATOR
    )
    assert created.status_code == 201, created.text
    return session_id, created.json()


def _join(session_id: str, code: str, client_id: str = "test"):
    response = client.post(
        f"/sessions/{session_id}/join", json={"join_code": code, "client_id": client_id}
    )
    client.cookies.clear()
    return response


def _token(session_id: str, code: str, client_id: str = "test") -> str:
    # Live tokens carry no nonce: the same role and client_id in the same
    # second mint the same token. A new device has its own client_id.
    joined = _join(session_id, code, client_id)
    assert joined.status_code == 200, joined.text
    return joined.json()["access_token"]


def _reissue(session_id: str, body: dict | None = None, headers=OPERATOR):
    if body is None:
        return client.post(f"/sessions/{session_id}/join-codes", headers=headers)
    return client.post(f"/sessions/{session_id}/join-codes", json=body, headers=headers)


def _chunk(session_id: str, token: str, sequence: int = 0):
    return client.post(
        f"/recording/{session_id}/chunk",
        data={"sequence": sequence},
        files={"chunk": (f"{sequence:06d}.webm", EBML, "video/webm")},
        headers=_bearer(token),
    )


def _audits(db, session_id: str, action: str) -> list[models.AuditLog]:
    db.expire_all()
    return list(
        db.scalars(
            select(models.AuditLog)
            .where(models.AuditLog.session_id == session_id)
            .where(models.AuditLog.action == action)
            .order_by(models.AuditLog.id)
        )
    )


def test_only_the_operator_may_reissue():
    session_id, created = _create()
    live = _token(session_id, created["join_codes"]["wizard"])
    assert _reissue(session_id, headers={}).status_code == 401
    assert _reissue(session_id, headers=_bearer(live)).status_code == 401
    assert _reissue("S-missing").status_code == 404


def test_default_replaces_all_three_and_old_codes_are_403_used_or_not():
    session_id, created = _create()
    old = created["join_codes"]
    student = _token(session_id, old["student"])  # used before the reissue

    response = _reissue(session_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"session_id", "join_codes", "revoke_live", "revoked_tokens"}
    assert set(body["join_codes"]) == {"student", "wizard", "teacher"}
    assert body["revoke_live"] is False and body["revoked_tokens"] == 0

    for role in ("student", "wizard", "teacher"):
        assert body["join_codes"][role] != old[role]
        stale = _join(session_id, old[role])
        assert stale.status_code == 403
        assert stale.json()["detail"] == "invalid join code"
    for role in ("student", "wizard", "teacher"):
        assert _join(session_id, body["join_codes"][role]).status_code == 200

    # Default keeps live tokens: the student already in the room carries on.
    assert client.get(f"/sessions/{session_id}", headers=_bearer(student)).status_code == 200


def test_roles_are_deduplicated_and_other_roles_keep_their_codes():
    session_id, created = _create()
    body = _reissue(session_id, {"roles": ["wizard", "wizard"]}).json()
    assert list(body["join_codes"]) == ["wizard"]
    assert _join(session_id, created["join_codes"]["wizard"]).status_code == 403
    assert _join(session_id, created["join_codes"]["student"]).status_code == 200


@pytest.mark.parametrize(
    "body",
    [{"roles": []}, {"roles": ["observer"]}, {"roles": ["admin"]}, {"revoke": True}],
)
def test_bad_bodies_are_422(body):
    session_id, _created = _create()
    assert _reissue(session_id, body).status_code == 422


def test_ended_session_is_409_session_ended():
    session_id, _created = _create()
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    response = _reissue(session_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "session_ended"


def test_rate_limit_is_its_own_budget():
    session_id, _created = _create()
    for _ in range(auth.JOIN_ISSUE_LIMIT):
        assert _reissue(session_id, {"roles": ["wizard"]}).status_code == 200
    limited = _reissue(session_id, {"roles": ["wizard"]})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate_limited"
    # Join reissues did not spend the upload-code budget.
    upload = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
    assert upload.status_code == 200, upload.text


def test_audit_carries_hashes_and_never_plaintext(db):
    session_id, created = _create()
    body = _reissue(session_id, {"roles": ["student", "teacher"]}).json()

    [entry] = _audits(db, session_id, models.AUDIT_JOIN_CODES_REISSUED)
    assert entry.actor_role == "operator"
    assert entry.details["roles"] == ["student", "teacher"]
    assert entry.details["revoke_live"] is False
    assert entry.details["revoked_tokens"] == 0
    for role in ("student", "teacher"):
        assert entry.details["old_code_hashes"][role] == models.hash_join_code(
            created["join_codes"][role]
        )
        assert entry.details["new_code_hashes"][role] == models.hash_join_code(
            body["join_codes"][role]
        )

    everything = json.dumps(
        [row.details for row in db.scalars(select(models.AuditLog))], default=str
    )
    for code in [*created["join_codes"].values(), *body["join_codes"].values()]:
        assert code not in everything


def test_revoke_live_cuts_only_the_chosen_roles_live_tokens(db):
    session_id, created = _create()
    codes = created["join_codes"]
    tokens = {role: _token(session_id, codes[role]) for role in ("student", "wizard", "teacher")}
    review = client.post(
        f"/sessions/{session_id}/review-login",
        json={"review_code": created["review_codes"]["teacher"]},
    )
    assert review.status_code == 200, review.text
    review_token = review.json()["access_token"]
    issued = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
    upload_login = client.post(
        f"/sessions/{session_id}/upload-login", json={"code": issued.json()["upload_code"]}
    )
    assert upload_login.status_code == 200, upload_login.text
    upload_token = upload_login.json()["access_token"]

    response = _reissue(session_id, {"roles": ["student"], "revoke_live": True})
    assert response.status_code == 200, response.text
    assert response.json()["revoked_tokens"] == 1

    # The old student live token is refused on REST and on the recording routes.
    old = client.get(f"/sessions/{session_id}", headers=_bearer(tokens["student"]))
    assert old.status_code == 401
    assert old.json()["detail"] == "revoked_session_token"
    assert _chunk(session_id, tokens["student"]).status_code == 401

    # Other roles, the review grant and the upload token are untouched.
    for role in ("wizard", "teacher"):
        assert client.get(f"/sessions/{session_id}", headers=_bearer(tokens[role])).status_code == 200
    status = client.get(f"/sessions/{session_id}/recording/status", headers=_bearer(review_token))
    assert status.status_code == 200, status.text
    assert _chunk(session_id, upload_token).status_code == 200

    # Not a security revocation: OP can still issue an upload code.
    assert models.upload_security_revoked(db, session_id) is False
    again = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
    assert again.status_code == 200, again.text

    revocations = _audits(db, session_id, models.AUDIT_TOKEN_REVOKED)
    assert [row.token_hash for row in revocations] == [models.hash_token(tokens["student"])]
    assert revocations[0].details == {"reason": models.REVOKE_REASON_JOIN_CODES_REISSUED}
    [entry] = _audits(db, session_id, models.AUDIT_JOIN_CODES_REISSUED)
    assert entry.details["revoke_live"] is True and entry.details["revoked_tokens"] == 1

    # The new code works and its token is live.
    fresh = _token(session_id, response.json()["join_codes"]["student"], "new-device")
    assert client.get(f"/sessions/{session_id}", headers=_bearer(fresh)).status_code == 200


def test_a_reissue_revocation_never_gets_the_grace_upload_exemption(db):
    session_id, created = _create()
    student = _token(session_id, created["join_codes"]["student"])
    _reissue(session_id, {"roles": ["student"], "revoke_live": True})
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200

    # /end skips tokens already revoked, and even if both reasons were on file
    # the stricter one wins, so the old token stays out during grace.
    models.revoke_token_hash(
        db,
        session_id,
        models.hash_token(student),
        reason=models.REVOKE_REASON_SESSION_ENDED,
    )
    assert (
        models.token_revocation_reason(db, session_id, models.hash_token(student))
        == models.REVOKE_REASON_JOIN_CODES_REISSUED
    )
    assert _chunk(session_id, student).status_code == 401


def _subprotocols(token: str) -> list[str]:
    return [WS_PROTOCOL, f"bearer.{token}"]


def _ping(sock, ping_id: str) -> None:
    sock.send_json({"type": "ping", "ping_id": ping_id, "client_timestamp_ms": 0})
    reply = sock.receive_json()
    while reply.get("type") != "pong":
        reply = sock.receive_json()
    assert reply["ping_id"] == ping_id


def test_revoke_live_closes_the_old_socket_only_after_commit_and_spares_others():
    session_id, created = _create()
    codes = created["join_codes"]
    student = _token(session_id, codes["student"])
    wizard = _token(session_id, codes["wizard"])

    with (
        client.websocket_connect(f"/ws/{session_id}/wizard", subprotocols=_subprotocols(wizard)) as wiz,
        client.websocket_connect(f"/ws/{session_id}/student", subprotocols=_subprotocols(student)) as old,
    ):
        _ping(old, "before")
        response = _reissue(session_id, {"roles": ["student"], "revoke_live": True})
        assert response.status_code == 200, response.text
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(20):
                old.send_json({"type": "ping", "ping_id": "after", "client_timestamp_ms": 0})
                old.receive_json()
        assert closed.value.code == 4401
        assert closed.value.reason == "token_revoked"
        _ping(wiz, "wizard-still-up")

    # The old token cannot open a new socket.
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(
            f"/ws/{session_id}/student", subprotocols=_subprotocols(student)
        ):
            pass
    assert refused.value.code == 4401

    # A device joining with the new code connects and stays up.
    fresh = _token(session_id, response.json()["join_codes"]["student"], "new-device")
    with client.websocket_connect(
        f"/ws/{session_id}/student", subprotocols=_subprotocols(fresh)
    ) as new:
        _ping(new, "new-device")


@pytest.fixture()
def file_db(monkeypatch, tmp_path):
    """Two real connections, so a reissue can commit between a join's SELECT and UPDATE."""
    engine = create_engine(f"sqlite:///{tmp_path / 'race.sqlite'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_a_join_that_read_the_old_code_cannot_claim_the_reissued_row(monkeypatch, file_db, role):
    setup = file_db()
    models.create_session(setup, "S-RACE", "CASE001")
    old = models.issue_join_codes(setup, "S-RACE")
    if role == "teacher":
        # Teacher codes are reusable; the repeat use must be guarded too.
        models.consume_join_code(setup, "S-RACE", old["teacher"])
    setup.close()

    fresh: dict[str, str] = {}
    real_update = models.update

    def reissue_in_between(*args, **kwargs):
        if not fresh:
            other = file_db()
            codes, _revoked = models.reissue_join_codes(
                other, "S-RACE", (role,), revoke_live=False
            )
            other.commit()
            other.close()
            fresh.update(codes)
        return real_update(*args, **kwargs)

    monkeypatch.setattr(models, "update", reissue_in_between)
    joiner = file_db()
    with pytest.raises(models.JoinCodeInvalid):
        models.consume_join_code(joiner, "S-RACE", old[role])
    joiner.close()
    monkeypatch.setattr(models, "update", real_update)

    check = file_db()
    row = check.scalars(
        select(models.JoinCode)
        .where(models.JoinCode.session_id == "S-RACE")
        .where(models.JoinCode.role == role)
    ).one()
    assert row.code_hash == models.hash_join_code(fresh[role])
    assert row.consumed_at is None
    assert models.consume_join_code(check, "S-RACE", fresh[role]).role == role
    check.close()
