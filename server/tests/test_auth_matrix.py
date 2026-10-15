"""ADR-0005 §5 endpoint x role matrix (session / case endpoints)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, cases, models, recording, websocket
from app.main import app
from app.models import Base

CASE_ID = "CASE001"
OP = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
NO_OP = "X-No-Operator"


def bearer(token: str) -> dict:
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


def make_session(client, session_id="S-M") -> dict[str, str]:
    created = client.post(
        "/sessions", json={"session_id": session_id, "case_id": CASE_ID}, headers=OP
    ).json()
    tokens = {}
    for role, code in created["join_codes"].items():
        resp = client.post(f"/sessions/{session_id}/join", json={"join_code": code})
        tokens[role] = resp.cookies[websocket.WS_SESSION_COOKIE]
        client.cookies.clear()
    return tokens


def test_missing_malformed_and_wrong_tokens_are_401(client):
    make_session(client)
    for headers in ({NO_OP: "1"}, bearer("garbage"), bearer("a.b")):
        assert client.get("/sessions/S-M", headers=headers).status_code == 401
        assert client.get("/sessions/S-M/events", headers=headers).status_code == 401
        assert client.post("/sessions/S-M/end", headers=headers).status_code == 401


def test_create_session_is_operator_only_and_leaks_no_join_codes(client):
    body = {"session_id": "S-X", "case_id": CASE_ID}
    for headers in (bearer("nope"), {NO_OP: "1"}):
        resp = client.post("/sessions", json=body, headers=headers)
        assert resp.status_code == 401
        assert "join_codes" not in resp.text
    tokens = make_session(client, "S-Y")
    resp = client.post(
        "/sessions", json={"session_id": "S-Z", "case_id": CASE_ID}, headers=bearer(tokens["wizard"])
    )
    assert resp.status_code == 401


def test_operator_token_unset_means_operator_endpoints_are_closed(client, monkeypatch):
    monkeypatch.delenv("MINDPROBE_OPERATOR_TOKEN")
    resp = client.post(
        "/sessions", json={"session_id": "S-N", "case_id": CASE_ID}, headers=OP
    )
    assert resp.status_code == 401


def test_roles_are_enforced_per_endpoint(client):
    t = make_session(client)
    assert client.get("/sessions/S-M", headers=bearer(t["student"])).status_code == 200
    assert client.get("/sessions/S-M/events", headers=bearer(t["student"])).status_code == 403
    assert client.get("/sessions/S-M/events", headers=bearer(t["wizard"])).status_code == 200
    assert client.post("/sessions/S-M/end", headers=bearer(t["student"])).status_code == 403
    # Operator may not read the review-only timeline.
    assert client.get("/sessions/S-M/timeline", headers=OP).status_code == 403


def test_token_bound_to_another_session_is_rejected(client):
    t = make_session(client, "S-A")
    make_session(client, "S-B")
    resp = client.get("/sessions/S-B", headers=bearer(t["wizard"]))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "session_token_mismatch"


def test_case_endpoints_hide_ground_truth_and_answers_from_students(client):
    t = make_session(client)
    assert client.get(f"/cases/{CASE_ID}", headers=bearer(t["student"])).status_code == 403
    assert client.get(f"/cases/{CASE_ID}", headers=bearer(t["wizard"])).status_code == 200
    clips = client.get(f"/cases/{CASE_ID}/clips", headers=bearer(t["student"])).json()["clips"]
    assert clips
    for clip in clips:
        assert set(clip) <= {"clip_id", "category", "character_state", "duration_ms", "file"}
    full = client.get(f"/cases/{CASE_ID}/clips", headers=bearer(t["wizard"])).json()["clips"]
    assert "text" in full[0]
    assert client.get("/cases", headers={NO_OP: "1"}).status_code == 401
    assert client.get("/cases", headers=bearer(t["student"])).status_code == 200


def test_session_ended_revocation_is_exempt_only_where_allowed(client):
    t = make_session(client)
    assert client.post("/sessions/S-M/end", headers=OP).status_code == 200
    assert client.get("/sessions/S-M/events", headers=bearer(t["teacher"])).status_code == 200
    assert client.get("/sessions/S-M", headers=bearer(t["teacher"])).status_code == 401
    assert client.get("/sessions/S-M/audit", headers=bearer(t["teacher"])).status_code == 401


def test_security_revoked_token_is_never_exempt(client, db):
    t = make_session(client)
    models.revoke_token_hash(db, "S-M", models.hash_token(t["teacher"]))
    assert client.post("/sessions/S-M/end", headers=OP).status_code == 200
    resp = client.get("/sessions/S-M/events", headers=bearer(t["teacher"]))
    assert resp.status_code == 401
    assert models.token_revocation_reason(
        db, "S-M", models.hash_token(t["teacher"])
    ) == models.REVOKE_REASON_SECURITY


def test_cookie_alone_is_rejected_when_cookie_auth_is_off(client, monkeypatch):
    t = make_session(client)
    cookie = {"Cookie": f"{websocket.WS_SESSION_COOKIE}={t['wizard']}"}
    assert client.get("/sessions/S-M/events", headers=cookie).status_code == 200
    monkeypatch.setenv("MINDPROBE_AUTH_COOKIE", "false")
    assert client.get("/sessions/S-M/events", headers=cookie).status_code == 401


def test_cors_does_not_allow_unlisted_origins(client):
    resp = client.options(
        "/sessions/S-M",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in resp.headers
    assert resp.headers.get("access-control-allow-credentials") != "true"


@pytest.mark.parametrize("ended", [False, True])
def test_revocation_cannot_be_bypassed_with_token_encoding(client, db, ended):
    token = make_session(client)["teacher"]
    assert client.get("/sessions/S-M/events", headers=bearer(token)).status_code == 200
    models.revoke_token_hash(db, "S-M", models.hash_token(token))
    if ended:
        assert client.post("/sessions/S-M/end", headers=OP).status_code == 200
    payload, signature = token.split(".")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    # SHA-256 has two unused bits in its last base64 character. A permissive
    # decoder accepts a different last character as the exact same signature.
    aliased = signature[:-1] + alphabet[alphabet.index(signature[-1]) + 1]
    assert websocket._b64decode(aliased) == websocket._b64decode(signature)
    variants = [
        token, token + "=", token + "==", token + "!",
        f"{payload}.{aliased}", f"{payload}=.{signature}",
    ]
    for variant in variants:
        for path in ("/sessions/S-M/events", "/sessions/S-M/recording/status", "/cases"):
            assert client.get(path, headers=bearer(variant)).status_code == 401


@pytest.mark.parametrize("token", [b"\xe9.x", b"x.\xe9", b"a.b.c", b".x", b"x."])
def test_malformed_token_bytes_return_401_not_500(client, token):
    # Raw header bytes reach ASGI's latin-1 decoding without HTTPX's ASCII guard.
    response = client.get(
        "/sessions/S-M/events",
        headers={b"Authorization": b"Bearer " + token, NO_OP: "1"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "malformed_session_token"


@pytest.mark.parametrize("offset", [-1, 0])
def test_expired_token_is_rejected(client, monkeypatch, offset):
    token = make_session(client)["teacher"]
    expiry = auth.decode_session_token(token)["exp"]
    monkeypatch.setattr(auth.time, "time", lambda: expiry - offset)
    response = client.get("/sessions/S-M/events", headers=bearer(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "expired_session_token"


def test_audit_is_operator_only_until_review_grants_exist(client):
    tokens = make_session(client)
    for token in tokens.values():
        assert client.get("/sessions/S-M/audit", headers=bearer(token)).status_code == 401
    assert client.get("/sessions/S-M/audit", headers={NO_OP: "1"}).status_code == 401
    assert client.get("/sessions/S-M/audit", headers=OP).status_code == 200
    # A signed typ=review alone is not a substitute for a validated grant.
    claims = auth.decode_session_token(tokens["teacher"])
    claims["typ"] = "review"
    payload = websocket._b64encode(json.dumps(claims).encode())
    signature = hmac.new(websocket._token_secret(), payload.encode("ascii"), hashlib.sha256)
    token = f"{payload}.{websocket._b64encode(signature.digest())}"
    assert client.get("/sessions/S-M/audit", headers=bearer(token)).status_code == 401


@pytest.mark.parametrize("environment", [None, "production", "development", "local"])
def test_case_files_are_not_public_but_local_media_is(client, monkeypatch, tmp_path, environment):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    if environment is None:
        monkeypatch.delenv("MINDPROBE_ENV", raising=False)
    else:
        monkeypatch.setenv("MINDPROBE_ENV", environment)
    case = tmp_path / CASE_ID
    media = case / "media"
    media.mkdir(parents=True)
    for filename in ("ground_truth.json", "wizard_decision_tree.json", "storyboard.md.bak"):
        (case / filename).write_text("sensitive")
        (media / filename).write_text("sensitive")
        for method in (client.get, client.head):
            for path in (f"/cases/{CASE_ID}/{filename}", f"/cases/{CASE_ID}/media/{filename}"):
                assert method(path, headers={NO_OP: "1"}).status_code == 404
    (media / "clip.mp4.bak").write_bytes(b"backup")
    assert client.get(f"/cases/{CASE_ID}/media/clip.mp4.bak", headers={NO_OP: "1"}).status_code == 404
    for filename in ("clip.mp4", "anchor.png"):
        (media / filename).write_bytes(b"media-bytes")
        response = client.get(f"/cases/{CASE_ID}/media/{filename}", headers={NO_OP: "1"})
        assert response.status_code == (200 if environment in {"local", "development"} else 404)
        if response.status_code == 200:
            assert response.content == b"media-bytes"
    (case / "secret.mp4").write_bytes(b"secret")
    for escape in ("%2e%2e%2fsecret.mp4", "%2e%2e%5csecret.mp4"):
        assert client.get(f"/cases/{CASE_ID}/media/{escape}", headers={NO_OP: "1"}).status_code == 404


@pytest.mark.parametrize("role", ["teacher", "observer"])
def test_recording_get_never_processes_chunks(client, monkeypatch, tmp_path, role):
    make_session(client)
    token = websocket.create_session_token("S-M", role)
    monkeypatch.setattr(recording, "mp4_path", lambda _: tmp_path / "student.mp4")
    monkeypatch.setattr(recording, "merged_path", lambda _: tmp_path / "student_raw.webm")
    monkeypatch.setattr(recording, "stored_chunks", lambda _: [tmp_path / "chunk.webm"])

    def forbidden(*args, **kwargs):
        pytest.fail("GET must not merge, transcode or bypass the header check")

    monkeypatch.setattr(recording, "process_recording", forbidden)
    response = client.get("/sessions/S-M/recording", headers=bearer(token))
    assert response.status_code == 404
