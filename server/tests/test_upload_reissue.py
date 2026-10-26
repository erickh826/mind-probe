"""Upload-code reissue: typ=upload default-deny, grant revoke, and case d."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool
from starlette.websockets import WebSocketDisconnect

from app import auth, cases, models, recording, websocket
from app.main import app
from app.models import Base

OPERATOR = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
EBML = recording.EBML_MAGIC + b"\x01\x00\x00\x00\x00\x00\x00\x23header"


@pytest.fixture(autouse=True)
def _reset_limits():
    auth.reset_upload_rate_limits()
    yield
    auth.reset_upload_rate_limits()


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
def chunk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path)
    return tmp_path


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _open(client: TestClient, session_id: str | None = None) -> tuple[str, dict[str, str], dict[str, str]]:
    session_id = session_id or f"S-{uuid4().hex[:8]}"
    created = client.post(
        "/sessions",
        json={"session_id": session_id, "case_id": "CASE001"},
        headers=OPERATOR,
    )
    assert created.status_code == 201, created.text
    codes = created.json()["join_codes"]
    tokens: dict[str, str] = {}
    for role, code in codes.items():
        joined = client.post(
            f"/sessions/{session_id}/join",
            json={"join_code": code, "client_id": f"{role}-test"},
        )
        assert joined.status_code == 200, joined.text
        tokens[role] = joined.json()["access_token"]
        client.cookies.clear()
    return session_id, tokens, codes


def _reissue(client: TestClient, session_id: str) -> dict:
    response = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
    assert response.status_code == 200, response.text
    return response.json()


def _redeem(client: TestClient, session_id: str, code: str):
    return client.post(
        f"/sessions/{session_id}/upload-login",
        json={"code": code},
    )


def _upload_token(client: TestClient, session_id: str) -> str:
    before_issue = int(time.time())
    body = _redeem(client, session_id, _reissue(client, session_id)["upload_code"])
    after_issue = int(time.time())
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["token_type"] == "upload"
    assert payload["role"] == "student"
    claims = auth.decode_session_token(payload["access_token"])
    assert claims["typ"] == "upload"
    assert claims["grant_id"] == session_id
    assert claims["role"] == "student"
    # Expiry is computed before signing sets iat. Crossing a wall-clock second
    # between those operations is legitimate; bound both to the actual request
    # interval instead of requiring two separate time() calls to be identical.
    assert before_issue + auth.UPLOAD_TOKEN_TTL_SECONDS <= claims["exp"] <= after_issue + auth.UPLOAD_TOKEN_TTL_SECONDS
    assert before_issue <= claims["iat"] <= after_issue
    assert 0 < claims["exp"] - claims["iat"] <= auth.UPLOAD_TOKEN_TTL_SECONDS
    return payload["access_token"]


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
        "total_duration_ms": 1000,
        **payload,
    }
    return client.post(
        f"/recording/{session_id}/finalize",
        json=body,
        headers=_bearer(token),
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/sessions/{sid}"),
        ("GET", "/sessions/{sid}/events"),
        ("POST", "/sessions/{sid}/end"),
        ("POST", "/sessions/{sid}/join"),
        ("POST", "/sessions/{sid}/upload-reissue"),
        ("GET", "/cases"),
        ("GET", "/cases/CASE001"),
        ("GET", "/cases/CASE001/clips"),
        ("GET", "/cases/CASE001/rubric"),
        ("GET", "/cases/CASE001/media/clip.mp4"),
        ("GET", "/sessions/{sid}/recording/status"),
        ("GET", "/sessions/{sid}/recording"),
        ("GET", "/sessions/{sid}/timeline"),
        ("GET", "/sessions/{sid}/audit"),
        ("POST", "/recording/{sid}/process"),
        ("POST", "/sessions"),
    ],
)
def test_upload_token_is_denied_outside_chunk_finalize_and_status(client, method, path):
    session_id, _tokens, _codes = _open(client)
    token = _upload_token(client, session_id)
    target = path.format(sid=session_id)
    kwargs = {"headers": _bearer(token)}
    if method == "POST" and target == "/sessions":
        kwargs["json"] = {"session_id": "S-other", "case_id": "CASE001"}
    elif method == "POST" and target.endswith("/join"):
        kwargs["json"] = {"join_code": "not-the-upload-code"}
    response = client.request(method, target, **kwargs)
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "token_type_mismatch"


def test_health_stays_public_with_an_upload_token(client):
    session_id, _tokens, _codes = _open(client)
    token = _upload_token(client, session_id)
    assert client.get("/health", headers=_bearer(token)).status_code == 200
    assert client.get("/health").status_code == 200


def test_upload_token_websocket_is_rejected(client):
    session_id, _tokens, _codes = _open(client)
    token = _upload_token(client, session_id)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/ws/{session_id}/student",
            headers={"cookie": f"{websocket.WS_SESSION_COOKIE}={token}"},
        ):
            pass
    assert exc.value.code == 4401
    assert exc.value.reason == "token_type_mismatch"


def test_live_student_cannot_issue_an_upload_code(client):
    session_id, tokens, _codes = _open(client)
    response = client.post(
        f"/sessions/{session_id}/upload-reissue",
        headers=_bearer(tokens["student"]),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_operator_token"


def test_upload_routes_accept_live_and_upload_tokens(client, chunk_root):
    session_id, tokens, _codes = _open(client)
    live = client.get(f"/recording/{session_id}/status", headers=_bearer(tokens["student"]))
    assert live.status_code == 200
    upload = _upload_token(client, session_id)
    assert client.get(f"/sessions/{session_id}", headers=_bearer(tokens["student"])).status_code == 200
    status = client.get(f"/recording/{session_id}/status", headers=_bearer(upload))
    assert status.status_code == 200
    assert set(status.json()) == {
        "received_sequences",
        "next_expected_sequence",
        "final_sequence",
        "finalize_state",
        "recording_state",
    }
    stored = _upload(client, session_id, upload, 0, EBML)
    assert stored.status_code == 200, stored.text
    done = _finalize(client, session_id, upload, final_sequence=0, total_chunks=1)
    assert done.status_code == 200, done.text


def test_reissue_before_end_and_during_grace_only(client, db):
    session_id, tokens, _codes = _open(client)
    for status in ("created", "starting", "active", "paused"):
        row = models.get_session(db, session_id)
        assert row is not None
        row.status = status
        db.commit()
        issued = _reissue(client, session_id)
        assert issued["recording_state"] is None
        assert issued["stored_sequences"] == 0
        assert issued["seconds_since_last_chunk"] is None
        assert "upload_code" in issued
    assert client.get("/sessions/" + session_id, headers=_bearer(tokens["student"])).status_code == 200
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    db.expire_all()
    grant = db.get(models.UploadGrant, session_id)
    assert grant is not None
    assert grant.security_revoked_at is None
    during = _reissue(client, session_id)
    redeemed = _redeem(client, session_id, during["upload_code"])
    assert redeemed.status_code == 200, redeemed.text
    claims = auth.decode_session_token(redeemed.json()["access_token"])
    session = models.get_session(db, session_id)
    assert session is not None and session.ended_at is not None
    grace_end = int(auth._as_utc(session.ended_at).timestamp()) + auth.upload_grace_seconds()
    assert claims["exp"] <= grace_end
    assert claims["exp"] > int(datetime.now(timezone.utc).timestamp())
    ended = session.ended_at
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    auth_now = ended + timedelta(seconds=auth.upload_grace_seconds() + 1)

    def later() -> datetime:
        return auth_now

    monkey_now = later
    # The HMAC exp still lies in the future; the grace recheck is what fails.
    token = redeemed.json()["access_token"]
    assert auth.decode_session_token(token)["exp"] > int(datetime.now(timezone.utc).timestamp())
    original = auth.utcnow
    auth.utcnow = monkey_now
    try:
        refused = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
        assert refused.status_code == 409
        assert refused.json()["detail"] == "upload_grace_expired"
        used = client.get(f"/recording/{session_id}/status", headers=_bearer(token))
        assert used.status_code == 401
        assert used.json()["detail"] == "upload_grace_expired"
        live = client.get(f"/recording/{session_id}/status", headers=_bearer(tokens["student"]))
        assert live.status_code == 401
        assert live.json()["detail"] == "revoked_session_token"
    finally:
        auth.utcnow = original


def test_end_does_not_block_grace_upload_and_security_revoke_does(client, db, chunk_root):
    session_id, tokens, _codes = _open(client)
    token = _upload_token(client, session_id)
    assert client.post(f"/sessions/{session_id}/end", headers=OPERATOR).status_code == 200
    db.expire_all()
    grant = db.get(models.UploadGrant, session_id)
    assert grant is not None and grant.security_revoked_at is None
    assert client.get(f"/recording/{session_id}/status", headers=_bearer(token)).status_code == 200
    assert client.get(
        f"/recording/{session_id}/status", headers=_bearer(tokens["student"])
    ).status_code == 200

    models.revoke_token_hash(db, session_id, models.hash_token(tokens["student"]))
    db.expire_all()
    grant = db.get(models.UploadGrant, session_id)
    assert grant is not None and grant.security_revoked_at is not None
    blocked = client.get(f"/recording/{session_id}/status", headers=_bearer(token))
    assert blocked.status_code == 401
    assert blocked.json()["detail"] == "revoked_session_token"
    again = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
    assert again.status_code == 409
    assert again.json()["detail"] == "upload_security_revoked"


def test_security_revoke_blocks_issue_and_redeem_without_consuming_the_code(client, db):
    session_id, tokens, _codes = _open(client)
    issued = _reissue(client, session_id)
    models.revoke_token_hash(db, session_id, models.hash_token(tokens["wizard"]))
    redeem = _redeem(client, session_id, issued["upload_code"])
    assert redeem.status_code == 409
    assert redeem.json()["detail"] == "upload_security_revoked"
    issue = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
    assert issue.status_code == 409
    db.expire_all()
    row = db.scalars(
        select(models.UploadCode).where(models.UploadCode.session_id == session_id)
    ).one()
    assert row.consumed_at is None
    assert models.upload_security_revoked(db, session_id)


def test_reissue_withdraws_the_previous_unused_code(client, db):
    session_id, _tokens, _codes = _open(client)
    first = _reissue(client, session_id)["upload_code"]
    second = _reissue(client, session_id)["upload_code"]
    stale = _redeem(client, session_id, first)
    assert stale.status_code == 409
    assert stale.json()["detail"] == "upload_code_consumed"
    fresh = _redeem(client, session_id, second)
    assert fresh.status_code == 200, fresh.text
    db.expire_all()
    audits = db.scalars(
        select(models.AuditLog).where(
            models.AuditLog.session_id == session_id,
            models.AuditLog.action == models.AUDIT_UPLOAD_CODE_ISSUED,
        )
    ).all()
    rendered = repr([row.details for row in audits])
    assert first not in rendered
    assert second not in rendered
    assert models.hash_join_code(second) in rendered


def test_cross_session_code_is_rejected_and_does_not_consume(client):
    session_a, _tokens_a, _codes_a = _open(client, "S-A")
    session_b, _tokens_b, _codes_b = _open(client, "S-B")
    code = _reissue(client, session_a)["upload_code"]
    crossed = _redeem(client, session_b, code)
    assert crossed.status_code == 401
    assert crossed.json()["detail"] == "upload_code_invalid"
    assert _redeem(client, session_a, code).status_code == 200


def test_upload_code_expires_at_ten_minutes_without_being_consumed(client, db, monkeypatch):
    session_id, _tokens, _codes = _open(client)
    code = _reissue(client, session_id)["upload_code"]
    db.expire_all()
    row = db.scalars(
        select(models.UploadCode).where(models.UploadCode.session_id == session_id)
    ).one()
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    lifetime = expires_at - (
        row.created_at.replace(tzinfo=timezone.utc)
        if row.created_at.tzinfo is None
        else row.created_at
    )
    assert timedelta(minutes=9) < lifetime <= timedelta(minutes=10, seconds=5)
    monkeypatch.setattr(auth, "utcnow", lambda: expires_at)
    expired = _redeem(client, session_id, code)
    assert expired.status_code == 401
    assert expired.json()["detail"] == "upload_code_expired"
    monkeypatch.setattr(auth, "utcnow", lambda: expires_at - timedelta(seconds=1))
    assert _redeem(client, session_id, code).status_code == 200


def test_issue_and_redeem_rate_limits(client):
    session_id, _tokens, _codes = _open(client)
    codes = [_reissue(client, session_id)["upload_code"] for _ in range(5)]
    limited = client.post(f"/sessions/{session_id}/upload-reissue", headers=OPERATOR)
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate_limited"
    assert _redeem(client, session_id, codes[-1]).status_code == 200
    auth.reset_upload_rate_limits()
    other, _tokens, _codes = _open(client, "S-rate")
    issued = _reissue(client, other)["upload_code"]
    for _ in range(5):
        bad = _redeem(client, other, "wrong-code")
        assert bad.status_code == 401
    blocked = _redeem(client, other, issued)
    assert blocked.status_code == 429
    auth.reset_upload_rate_limits()
    assert _redeem(client, other, issued).status_code == 200


def test_concurrent_redeem_succeeds_once(tmp_path):
    auth.reset_upload_rate_limits()
    engine = create_engine(
        f"sqlite:///{tmp_path}/redeem.sqlite",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[models.get_db] = override
    client = TestClient(app)
    try:
        session_id, _tokens, _codes = _open(client, "S-race")
        code = _reissue(client, session_id)["upload_code"]
        barrier = threading.Barrier(2)
        statuses: list[int] = []
        lock = threading.Lock()

        def redeem() -> None:
            barrier.wait(timeout=5)
            response = client.post(
                f"/sessions/{session_id}/upload-login",
                json={"code": code},
            )
            with lock:
                statuses.append(response.status_code)

        threads = [threading.Thread(target=redeem) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(statuses) == [200, 409]
    finally:
        app.dependency_overrides.pop(models.get_db, None)
        engine.dispose()


def test_case_d_upload_token_finishes_the_same_take_and_recorder_lost_stays(client, db, chunk_root):
    session_id, tokens, codes = _open(client)
    assert _upload(client, session_id, tokens["student"], 0, EBML).status_code == 200
    replay = client.post(
        f"/sessions/{session_id}/join",
        json={"join_code": codes["student"]},
    )
    assert replay.status_code == 409
    facts = _reissue(client, session_id)
    assert facts["recording_state"] == "recording"
    assert facts["stored_sequences"] == 1
    assert isinstance(facts["seconds_since_last_chunk"], int)
    redeemed = _redeem(client, session_id, facts["upload_code"])
    assert redeemed.status_code == 200
    upload = redeemed.json()["access_token"]
    assert _upload(client, session_id, upload, 1, b"\x1a\x45\xdf\xa3tail").status_code == 200
    done = _finalize(
        client,
        session_id,
        upload,
        final_sequence=1,
        total_chunks=2,
        total_duration_ms=2000,
    )
    assert done.status_code == 200, done.text
    assert done.json()["finalize_state"] == "complete"
    assert client.get(f"/sessions/{session_id}", headers=_bearer(upload)).status_code == 401
    db.expire_all()
    assert [row.id for row in db.scalars(select(models.Session)).all()] == [session_id]

    lost_id, lost_tokens, _lost_codes = _open(client, "S-lost")
    assert _upload(client, lost_id, lost_tokens["student"], 0, EBML).status_code == 200
    lost = _finalize(
        client,
        lost_id,
        lost_tokens["student"],
        reason="recorder_lost",
    )
    assert lost.status_code == 200
    assert lost.json()["recording_state"] == "incomplete"
    lost_upload = _upload_token(client, lost_id)
    again = _finalize(client, lost_id, lost_upload, reason="recorder_lost")
    assert again.status_code == 200
    assert again.json()["recording_state"] == "incomplete"
    db.expire_all()
    recording_row = models.get_recording(db, lost_id)
    assert recording_row is not None
    assert recording_row.incomplete_reason == "recorder_lost"
    assert recording_row.recording_state == "incomplete"


ALLOWED_ORIGIN = "https://student.example"
BLOCKED_ORIGIN = "https://evil.example"


def _origin(origin: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Origin": origin}
    if extra:
        headers.update(extra)
    return headers


def test_early_auth_errors_carry_cors_for_an_allowed_origin(client, monkeypatch):
    """A short-circuit 401 must not look like a network failure to the browser."""
    monkeypatch.setenv("MINDPROBE_CORS_ORIGINS", ALLOWED_ORIGIN)
    session_id, _tokens, _codes = _open(client)
    token = _upload_token(client, session_id)

    health = client.get("/health", headers=_origin(ALLOWED_ORIGIN))
    assert health.status_code == 200
    assert health.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert health.headers.get("access-control-allow-credentials") != "true"

    preflight = client.options(
        f"/sessions/{session_id}",
        headers=_origin(
            ALLOWED_ORIGIN,
            {
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        ),
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert "authorization" in preflight.headers.get("access-control-allow-headers", "").lower()

    denied = client.get(
        f"/sessions/{session_id}",
        headers=_origin(ALLOWED_ORIGIN, _bearer(token)),
    )
    assert denied.status_code == 401
    assert denied.json()["detail"] == "token_type_mismatch"
    assert denied.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    missing = client.get(
        f"/recording/{session_id}/status",
        headers=_origin(ALLOWED_ORIGIN, {"X-No-Operator": "1"}),
    )
    assert missing.status_code == 401
    assert missing.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    garbage = client.get(
        f"/sessions/{session_id}",
        headers=_origin(ALLOWED_ORIGIN, _bearer("not-a-token")),
    )
    assert garbage.status_code == 401
    assert garbage.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_disallowed_origin_gets_no_cors_on_success_preflight_or_auth_error(client, monkeypatch):
    monkeypatch.setenv("MINDPROBE_CORS_ORIGINS", ALLOWED_ORIGIN)
    session_id, _tokens, _codes = _open(client)
    token = _upload_token(client, session_id)

    health = client.get("/health", headers=_origin(BLOCKED_ORIGIN))
    assert health.status_code == 200
    assert "access-control-allow-origin" not in health.headers

    preflight = client.options(
        f"/sessions/{session_id}",
        headers=_origin(
            BLOCKED_ORIGIN,
            {"Access-Control-Request-Method": "GET"},
        ),
    )
    assert "access-control-allow-origin" not in preflight.headers
    assert preflight.headers.get("access-control-allow-credentials") != "true"

    denied = client.get(
        f"/sessions/{session_id}",
        headers=_origin(BLOCKED_ORIGIN, _bearer(token)),
    )
    assert denied.status_code == 401
    assert "access-control-allow-origin" not in denied.headers

    missing = client.get(
        f"/recording/{session_id}/status",
        headers=_origin(BLOCKED_ORIGIN, {"X-No-Operator": "1"}),
    )
    assert missing.status_code == 401
    assert "access-control-allow-origin" not in missing.headers


def test_local_case_media_stays_public_and_rejects_an_upload_token(client, monkeypatch, tmp_path):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setenv("MINDPROBE_ENV", "local")
    media = tmp_path / "CASE001" / "media"
    media.mkdir(parents=True)
    (media / "clip.mp4").write_bytes(b"media-bytes")
    session_id, _tokens, _codes = _open(client)
    token = _upload_token(client, session_id)
    denied = client.get("/cases/CASE001/media/clip.mp4", headers=_bearer(token))
    assert denied.status_code == 401
    assert denied.json()["detail"] == "token_type_mismatch"
    opened = client.get("/cases/CASE001/media/clip.mp4", headers={"X-No-Operator": "1"})
    assert opened.status_code == 200
    assert opened.content == b"media-bytes"
