"""Review grants: codes, login, refresh rotation, logout, and revoke (ADR-0005 §6.5)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app import auth, models, websocket
from app.main import app
from app.models import Base

CASE_ID = "CASE001"
OP = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
NO_OP = {"X-No-Operator": "1"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_review_limits():
    auth.reset_review_rate_limits()
    yield
    auth.reset_review_rate_limits()


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
    try:
        yield session
    finally:
        session.close()


def create(client: TestClient, session_id: str = "S-R") -> dict:
    response = client.post(
        "/sessions",
        json={"session_id": session_id, "case_id": CASE_ID},
        headers=OP,
    )
    assert response.status_code == 201, response.text
    return response.json()


def review_login(client: TestClient, session_id: str, code: str, client_id: str = "device-1"):
    return client.post(
        f"/sessions/{session_id}/review-login",
        json={"review_code": code, "client_id": client_id},
    )


def review_refresh(client: TestClient, session_id: str, refresh_token: str):
    return client.post(
        f"/sessions/{session_id}/review-refresh",
        json={"refresh_token": refresh_token},
    )


def protocols(token: str) -> list[str]:
    return [websocket.WS_PROTOCOL, f"bearer.{token}"]


def test_create_session_returns_review_codes_once_and_stores_hashes(client, db):
    created = create(client)
    codes = created["review_codes"]
    assert set(codes) == {"teacher", "observer"}
    assert codes["teacher"] != codes["observer"]
    for raw in codes.values():
        assert raw not in created["join_codes"].values()

    fetched = client.get("/sessions/S-R", headers=OP)
    assert fetched.status_code == 200
    assert "review_codes" not in fetched.json()
    assert "join_codes" not in fetched.json()

    db.rollback()
    rows = db.scalars(select(models.ReviewCode).where(models.ReviewCode.session_id == "S-R")).all()
    assert {row.role for row in rows} == {"teacher", "observer"}
    stored = {row.role: row.code_hash for row in rows}
    for role, raw in codes.items():
        assert raw not in stored[role]
        assert stored[role] == models.hash_join_code(raw)


def test_teacher_and_observer_can_review_an_in_progress_session(client, db):
    created = create(client)
    for role in ("teacher", "observer"):
        logged = review_login(client, "S-R", created["review_codes"][role], client_id=f"{role}-device")
        assert logged.status_code == 200, logged.text
        assert websocket.WS_SESSION_COOKIE not in logged.cookies
        body = logged.json()
        assert body["role"] == role
        assert body["access_token"] != body["refresh_token"]
        delta = body["expires_at"] - int(time.time())
        assert 1700 <= delta <= 1800
        token = body["access_token"]
        assert client.get("/sessions/S-R", headers=bearer(token)).status_code == 200
        assert client.get("/sessions/S-R/events", headers=bearer(token)).status_code == 200
        assert client.get("/sessions/S-R/timeline", headers=bearer(token)).status_code == 200
        assert client.get("/sessions/S-R/recording/status", headers=bearer(token)).status_code == 200
        assert client.get(f"/cases/{CASE_ID}/rubric", headers=bearer(token)).status_code == 200
        claims = auth.decode_session_token(token)
        assert claims["typ"] == "review"
        assert claims["sub"] == f"{role}-device"
        db.rollback()
        grant = db.get(models.ReviewGrant, claims["jti"])
        assert grant is not None
        assert grant.client_id == f"{role}-device"
        assert grant.refresh_hash == models.hash_token(body["refresh_token"])
        assert grant.revoked_at is None
        assert abs((grant.expires_at - grant.issued_at).total_seconds() - 604800) < 2
        assert body["refresh_token"] not in grant.refresh_hash

    with client.websocket_connect(
        "/ws/S-R/teacher",
        subprotocols=protocols(
            review_login(client, "S-R", created["review_codes"]["teacher"]).json()["access_token"]
        ),
    ) as sock:
        assert sock.accepted_subprotocol == websocket.WS_PROTOCOL
        sock.send_json({"type": "ping", "ping_id": "review", "client_timestamp_ms": 0})
        assert sock.receive_json()["ping_id"] == "review"


def test_review_codes_are_not_join_codes(client):
    created = create(client)
    joined = client.post(
        "/sessions/S-R/join",
        json={"join_code": created["review_codes"]["teacher"], "client_id": "nope"},
    )
    assert joined.status_code == 403
    reviewed = review_login(client, "S-R", created["join_codes"]["teacher"])
    assert reviewed.status_code == 401
    assert reviewed.json()["detail"] == "invalid_review_code"


def test_regenerate_review_codes_invalidates_old_codes_not_existing_grants(client):
    created = create(client)
    old_teacher = created["review_codes"]["teacher"]
    logged = review_login(client, "S-R", old_teacher)
    assert logged.status_code == 200
    access = logged.json()["access_token"]

    denied = client.post("/sessions/S-R/review-codes", headers=NO_OP)
    assert denied.status_code == 401
    regenerated = client.post("/sessions/S-R/review-codes", headers=OP)
    assert regenerated.status_code == 200, regenerated.text
    fresh = regenerated.json()["review_codes"]
    assert fresh["teacher"] != old_teacher
    assert fresh["observer"] != created["review_codes"]["observer"]
    assert review_login(client, "S-R", old_teacher).status_code == 401
    assert client.get("/sessions/S-R/timeline", headers=bearer(access)).status_code == 200
    again = review_login(client, "S-R", fresh["teacher"])
    assert again.status_code == 200


def test_refresh_rotation_and_reuse_revokes_the_chain(client):
    created = create(client)
    first = review_login(client, "S-R", created["review_codes"]["teacher"]).json()
    second = review_refresh(client, "S-R", first["refresh_token"])
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["access_token"] != first["access_token"]
    assert second_body["refresh_token"] != first["refresh_token"]
    third = review_refresh(client, "S-R", second_body["refresh_token"])
    assert third.status_code == 200, third.text
    third_body = third.json()

    # The current access tokens still work. Only a reused refresh kills the chain.
    assert client.get("/sessions/S-R/events", headers=bearer(third_body["access_token"])).status_code == 200
    assert client.get("/sessions/S-R/events", headers=bearer(first["access_token"])).status_code == 200

    reused = review_refresh(client, "S-R", first["refresh_token"])
    assert reused.status_code == 401
    assert reused.json()["detail"] == "refresh_reused"
    for token in (first["access_token"], second_body["access_token"], third_body["access_token"]):
        for path in ("/events", "/timeline", "/recording"):
            response = client.get(f"/sessions/S-R{path}", headers=bearer(token))
            assert response.status_code == 401, path
            assert response.json()["detail"] == "revoked_session_token"
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/ws/S-R/teacher", subprotocols=protocols(third_body["access_token"])
        ):
            pass
    assert exc.value.code == 4401
    assert exc.value.reason == "token_revoked"

    # The rotated refresh is dead too. Presenting it does not mint another grant.
    assert review_refresh(client, "S-R", third_body["refresh_token"]).status_code == 401


def test_refresh_for_another_session_does_not_revoke(client):
    created = create(client, "S-A")
    create(client, "S-B")
    logged = review_login(client, "S-A", created["review_codes"]["observer"]).json()
    foreign = review_refresh(client, "S-B", logged["refresh_token"])
    assert foreign.status_code == 401
    assert foreign.json()["detail"] == "invalid_refresh_token"
    rotated = review_refresh(client, "S-A", logged["refresh_token"])
    assert rotated.status_code == 200, rotated.text


def test_logout_and_operator_revoke_are_immediate(client):
    created = create(client)
    teacher = review_login(client, "S-R", created["review_codes"]["teacher"]).json()
    observer = review_login(client, "S-R", created["review_codes"]["observer"]).json()
    student = client.post(
        "/sessions/S-R/join",
        json={"join_code": created["join_codes"]["student"], "client_id": "student-1"},
    )
    assert student.status_code == 200, student.text
    student_token = student.json()["access_token"]
    client.cookies.clear()

    with (
        client.websocket_connect(
            "/ws/S-R/student", subprotocols=protocols(student_token)
        ) as student_ws,
        client.websocket_connect(
            "/ws/S-R/teacher", subprotocols=protocols(teacher["access_token"])
        ) as teacher_ws,
    ):
        teacher_ws.send_json({"type": "ping", "ping_id": "before", "client_timestamp_ms": 0})
        assert teacher_ws.receive_json()["ping_id"] == "before"
        logged_out = client.post(
            "/sessions/S-R/review-logout", headers=bearer(teacher["access_token"])
        )
        assert logged_out.status_code == 200, logged_out.text
        assert client.get(
            "/sessions/S-R/timeline", headers=bearer(teacher["access_token"])
        ).status_code == 401
        with pytest.raises(WebSocketDisconnect) as exc:
            teacher_ws.send_json({"type": "ping", "ping_id": "after", "client_timestamp_ms": 0})
            teacher_ws.receive_json()
        assert exc.value.code == 4401
        assert exc.value.reason == "token_revoked"
        student_ws.send_json({"type": "ping", "ping_id": "still", "client_timestamp_ms": 0})
        assert student_ws.receive_json()["ping_id"] == "still"

    # Observer was a different chain and still works until the operator revoke.
    assert client.get("/sessions/S-R/events", headers=bearer(observer["access_token"])).status_code == 200
    revoked = client.post("/sessions/S-R/review-revoke", headers=OP)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_grants"] >= 1
    assert client.get(
        "/sessions/S-R/recording/status", headers=bearer(observer["access_token"])
    ).status_code == 401
    assert client.post("/sessions/S-R/review-revoke", headers=NO_OP).status_code == 401
    # Codes are still reusable after the grants are revoked.
    assert review_login(client, "S-R", created["review_codes"]["observer"]).status_code == 200


def test_ended_session_review_login_has_no_live_exemption(client):
    created = create(client)
    live = client.post(
        "/sessions/S-R/join",
        json={"join_code": created["join_codes"]["teacher"], "client_id": "live-teacher"},
    )
    assert live.status_code == 200, live.text
    live_token = live.json()["access_token"]
    client.cookies.clear()
    assert client.post("/sessions/S-R/end", headers=OP).status_code == 200

    logged = review_login(client, "S-R", created["review_codes"]["teacher"])
    assert logged.status_code == 200, logged.text
    access = logged.json()["access_token"]
    assert client.get("/sessions/S-R", headers=bearer(access)).status_code == 200
    assert client.get("/sessions/S-R/events", headers=bearer(access)).status_code == 200
    assert client.get("/sessions/S-R/timeline", headers=bearer(access)).status_code == 200
    # The ended live token still has the read exemption. The review token does not use it.
    assert client.get("/sessions/S-R/events", headers=bearer(live_token)).status_code == 200
    assert client.get("/sessions/S-R", headers=bearer(live_token)).status_code == 401
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/S-R/teacher", subprotocols=protocols(access)):
            pass
    assert exc.value.code == 4401
    assert exc.value.reason == "session_ended"

    assert client.post("/sessions/S-R/review-logout", headers=bearer(access)).status_code == 200
    refused = client.get("/sessions/S-R/events", headers=bearer(access))
    assert refused.status_code == 401
    assert refused.json()["detail"] == "revoked_session_token"
    # Ending the session did not exempt the revoked review grant.
    assert client.get("/sessions/S-R/timeline", headers=bearer(access)).status_code == 401


def test_review_login_is_rate_limited_per_ip(client, monkeypatch):
    created = create(client)
    code = created["review_codes"]["teacher"]
    clock = {"now": 10_000.0}
    monkeypatch.setattr(auth, "review_rate_now", lambda: clock["now"])
    for _ in range(5):
        failed = review_login(client, "S-R", "not-a-code")
        assert failed.status_code == 401
    blocked = review_login(client, "S-R", code)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "rate_limited"
    clock["now"] += 61
    assert review_login(client, "S-R", code).status_code == 200


def test_upload_and_review_tokens_do_not_cross(client):
    created = create(client)
    reissued = client.post("/sessions/S-R/upload-reissue", headers=OP)
    assert reissued.status_code == 200, reissued.text
    uploaded = client.post(
        "/sessions/S-R/upload-login",
        json={"code": reissued.json()["upload_code"]},
    )
    assert uploaded.status_code == 200, uploaded.text
    upload_token = uploaded.json()["access_token"]
    assert uploaded.json()["token_type"] == "upload"
    review = review_login(client, "S-R", created["review_codes"]["teacher"]).json()

    timeline = client.get("/sessions/S-R/timeline", headers=bearer(upload_token))
    assert timeline.status_code == 401
    assert timeline.json()["detail"] == "token_type_mismatch"
    assert client.get("/sessions/S-R/timeline", headers=bearer(review["access_token"])).status_code == 200

    student_status = client.get("/recording/S-R/status", headers=bearer(review["access_token"]))
    assert student_status.status_code == 401
    assert student_status.json()["detail"] == "token_type_mismatch"
    assert client.get("/recording/S-R/status", headers=bearer(upload_token)).status_code == 200
    assert client.post("/sessions/S-R/end", headers=bearer(review["access_token"])).status_code == 401
    assert client.post(
        "/sessions/S-R/review-logout", headers=bearer(upload_token)
    ).status_code == 401


def test_expired_review_access_can_refresh_until_the_grant_is_revoked(client, monkeypatch, db):
    created = create(client)
    logged = review_login(client, "S-R", created["review_codes"]["observer"]).json()
    clock = {"now": None}
    real_time = time.time

    def fake_time() -> float:
        return clock["now"] if clock["now"] is not None else real_time()

    monkeypatch.setattr(auth.time, "time", fake_time)
    clock["now"] = logged["expires_at"] + 5
    expired = client.get("/sessions/S-R/events", headers=bearer(logged["access_token"]))
    assert expired.status_code == 401
    assert expired.json()["detail"] == "expired_session_token"
    refreshed = review_refresh(client, "S-R", logged["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    clock["now"] = None
    assert client.get(
        "/sessions/S-R/events", headers=bearer(refreshed.json()["access_token"])
    ).status_code == 200

    db.rollback()
    grant = db.scalars(select(models.ReviewGrant).where(models.ReviewGrant.session_id == "S-R")).all()
    current = next(row for row in grant if row.revoked_at is None and row.rotated_from is not None)
    current.expires_at = auth.utcnow() - timedelta(seconds=5)
    db.commit()
    stale = client.get(
        "/sessions/S-R/timeline", headers=bearer(refreshed.json()["access_token"])
    )
    assert stale.status_code == 401
    assert stale.json()["detail"] == "expired_session_token"


def test_review_audit_records_login_refresh_logout_and_revoke_without_secrets(client):
    created = create(client)
    logged = review_login(client, "S-R", created["review_codes"]["teacher"]).json()
    refreshed = review_refresh(client, "S-R", logged["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    assert client.post(
        "/sessions/S-R/review-logout", headers=bearer(refreshed.json()["access_token"])
    ).status_code == 200
    assert client.post("/sessions/S-R/review-revoke", headers=OP).status_code == 200

    audit = client.get("/sessions/S-R/audit", headers=bearer(logged["access_token"]))
    assert audit.status_code == 401
    # A fresh teacher grant can read the audit. Observer review and live tokens cannot.
    teacher = review_login(client, "S-R", created["review_codes"]["teacher"]).json()
    observer = review_login(client, "S-R", created["review_codes"]["observer"]).json()
    assert client.get("/sessions/S-R/audit", headers=bearer(observer["access_token"])).status_code == 403
    live = client.post(
        "/sessions/S-R/join",
        json={"join_code": created["join_codes"]["wizard"], "client_id": "wiz"},
    )
    client.cookies.clear()
    assert client.get("/sessions/S-R/audit", headers=bearer(live.json()["access_token"])).status_code == 401
    body = client.get("/sessions/S-R/audit", headers=bearer(teacher["access_token"]))
    assert body.status_code == 200, body.text
    actions = [entry["action"] for entry in body.json()["entries"]]
    assert "review_login" in actions
    assert "review_refresh" in actions
    assert "review_logout" in actions
    assert "review_revoke" in actions
    serialized = body.text
    for secret in (
        created["review_codes"]["teacher"],
        created["review_codes"]["observer"],
        logged["access_token"],
        logged["refresh_token"],
        refreshed.json()["refresh_token"],
        teacher["access_token"],
        teacher["refresh_token"],
    ):
        assert secret not in serialized


def test_signed_review_token_without_a_grant_is_rejected(client):
    created = create(client)
    live = client.post(
        "/sessions/S-R/join",
        json={"join_code": created["join_codes"]["teacher"], "client_id": "live"},
    )
    assert live.status_code == 200, live.text
    claims = auth.decode_session_token(live.json()["access_token"])
    claims["typ"] = "review"
    claims["jti"] = "missing-grant"
    payload = websocket._b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = hmac.new(
        websocket._token_secret(), payload.encode("ascii"), hashlib.sha256
    )
    forged = f"{payload}.{websocket._b64encode(signature.digest())}"
    response = client.get("/sessions/S-R/timeline", headers=bearer(forged))
    assert response.status_code == 401
    assert response.json()["detail"] == "revoked_session_token"


def test_review_access_ttl_env_is_honored(client, monkeypatch):
    monkeypatch.setenv("MINDPROBE_REVIEW_ACCESS_TTL", "120")
    created = create(client)
    logged = review_login(client, "S-R", created["review_codes"]["teacher"])
    assert logged.status_code == 200, logged.text
    delta = logged.json()["expires_at"] - int(time.time())
    assert 100 <= delta <= 120
