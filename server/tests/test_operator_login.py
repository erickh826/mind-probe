"""OP admin login: ``POST /op/login`` and ``POST /op/logout``.

A login token is a second operator credential next to the static
``MINDPROBE_OPERATOR_TOKEN``. It works wherever the static token works, expires
after ``MINDPROBE_OPERATOR_SESSION_TTL``, dies on logout, and never passes as a
session token or on a WebSocket.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import admin_password, auth, models, websocket
from app.main import app
from app.models import Base
from .ws_assert import assert_upgrade_refused

STATIC = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
USERNAME = "admin"
PASSWORD = "correct horse battery staple"
# One hash for the module: scrypt is deliberately slow.
PASSWORD_HASH = admin_password.hash_password(PASSWORD)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setenv(auth.ADMIN_USERNAME_ENV, USERNAME)
    monkeypatch.setenv(auth.ADMIN_PASSWORD_HASH_ENV, PASSWORD_HASH)
    auth.reset_operator_rate_limits()
    yield
    auth.reset_operator_rate_limits()


@pytest.fixture()
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # WebSocket upgrades open their own session through SessionLocal.
    monkeypatch.setattr(models, "SessionLocal", factory)
    yield factory
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


def login(client: TestClient, username: str = USERNAME, password: str = PASSWORD):
    return client.post("/op/login", json={"username": username, "password": password})


def token_of(client: TestClient) -> str:
    response = login(client)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def operator_audits(db) -> list[models.AuditLog]:
    return list(
        db.scalars(
            select(models.AuditLog)
            .where(models.AuditLog.session_id == auth.OPERATOR_AUDIT_SESSION_ID)
            .order_by(models.AuditLog.id)
        )
    )


def test_login_returns_an_expiring_operator_token_and_audits_without_plaintext(client, db):
    before = int(time.time())
    response = login(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "operator"
    assert body["role"] == "operator"
    ttl = auth.DEFAULT_OPERATOR_SESSION_TTL_SECONDS
    assert before + ttl <= body["expires_at"] <= int(time.time()) + ttl

    rows = operator_audits(db)
    assert [(row.action, row.details) for row in rows] == [
        ("operator_login", {"result": "ok"})
    ]
    assert rows[0].token_hash == models.hash_token(body["access_token"])
    for row in rows:
        assert body["access_token"] not in str(row.details)
        assert PASSWORD not in str(row.details)


def test_ttl_comes_from_the_env(client, monkeypatch):
    monkeypatch.setenv(auth.OPERATOR_SESSION_TTL_ENV, "120")
    body = login(client).json()
    assert body["expires_at"] - int(time.time()) in range(118, 121)


@pytest.mark.parametrize(
    "username,password",
    [(USERNAME, "wrong password"), ("someone", PASSWORD), ("", PASSWORD), (USERNAME, "")],
)
def test_wrong_credentials_are_401_and_audited(client, db, username, password):
    response = login(client, username, password)
    if not username or not password:
        assert response.status_code == 422
        return
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_operator_credentials"
    assert [(row.action, row.details, row.token_hash) for row in operator_audits(db)] == [
        ("operator_login", {"result": "invalid_credentials"}, None)
    ]


@pytest.mark.parametrize("unset", [auth.ADMIN_USERNAME_ENV, auth.ADMIN_PASSWORD_HASH_ENV])
def test_unconfigured_admin_refuses_every_login(client, monkeypatch, unset):
    monkeypatch.delenv(unset)
    response = login(client)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_operator_credentials"


def test_malformed_hash_refuses_login(client, monkeypatch):
    monkeypatch.setenv(auth.ADMIN_PASSWORD_HASH_ENV, "plaintext-password")
    assert login(client, password="plaintext-password").status_code == 401


def test_extra_fields_are_rejected(client):
    response = client.post(
        "/op/login", json={"username": USERNAME, "password": PASSWORD, "role": "admin"}
    )
    assert response.status_code == 422


def test_login_attempts_are_rate_limited_per_client(client, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(auth, "operator_rate_now", lambda: now[0])
    for _ in range(auth.OPERATOR_LOGIN_LIMIT):
        assert login(client, password="wrong").status_code == 401
    # Even the right password waits out the window.
    limited = login(client)
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate_limited"
    now[0] += auth.OPERATOR_LOGIN_WINDOW_SECONDS
    assert login(client).status_code == 200


def test_login_token_works_wherever_the_static_token_does(client):
    token = token_of(client)
    created = client.post(
        "/sessions", json={"session_id": "S-OP", "case_id": "CASE001"}, headers=bearer(token)
    )
    assert created.status_code == 201, created.text
    # require_operator routes, through the real middleware stack.
    assert client.get("/sessions", headers=bearer(token)).status_code == 200
    assert client.get("/sessions/S-OP/recording/status", headers=bearer(token)).status_code == 200
    reissued = client.post(
        "/sessions/S-OP/join-codes", json={"roles": ["wizard"]}, headers=bearer(token)
    )
    assert reissued.status_code == 200, reissued.text
    assert client.post("/sessions/S-OP/review-codes", headers=bearer(token)).status_code == 200
    # Session-scoped routes that admit the operator, and the pathless GET /cases.
    assert client.get("/sessions/S-OP", headers=bearer(token)).status_code == 200
    assert client.get("/sessions/S-OP/events", headers=bearer(token)).status_code == 200
    assert client.get("/sessions/S-OP/audit", headers=bearer(token)).status_code == 200
    assert client.get("/cases", headers=bearer(token)).status_code == 200
    assert client.post("/sessions/S-OP/end", headers=bearer(token)).status_code == 200


def test_login_token_is_refused_where_the_static_token_is(client):
    token = token_of(client)
    created = client.post("/sessions", json={"session_id": "S-OP", "case_id": "CASE001"}, headers=STATIC)
    assert created.status_code == 201
    # Teacher-only reads refuse the operator, whichever credential it uses.
    for path in ("/sessions/S-OP/timeline",):
        static = client.get(path, headers=STATIC)
        login_token = client.get(path, headers=bearer(token))
        assert static.status_code == 403, path
        assert (login_token.status_code, login_token.json()) == (static.status_code, static.json())
    review_logout = client.post("/sessions/S-OP/review-logout", headers=bearer(token))
    assert review_logout.status_code == client.post("/sessions/S-OP/review-logout", headers=STATIC).status_code
    # Never a WebSocket credential: wrong session binding for every role.
    for role in ("student", "wizard", "teacher", "observer"):
        assert_upgrade_refused(
            client.websocket_connect(
                f"/ws/S-OP/{role}", subprotocols=[websocket.WS_PROTOCOL, f"bearer.{token}"]
            ),
            code=4401,
        )


def test_reserved_operator_session_id_cannot_be_created(client):
    response = client.post(
        "/sessions",
        json={"session_id": auth.OPERATOR_AUDIT_SESSION_ID, "case_id": "CASE001"},
        headers=STATIC,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "reserved_session_id"


def test_expired_login_token_is_401_expired(client, monkeypatch):
    token = token_of(client)
    later = time.time() + auth.DEFAULT_OPERATOR_SESSION_TTL_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    for request in (
        lambda: client.get("/sessions", headers=bearer(token)),
        lambda: client.get("/cases", headers=bearer(token)),
    ):
        response = request()
        assert response.status_code == 401
        assert response.json()["detail"] == "expired_session_token"


def test_logout_revokes_only_that_login_token(client, db):
    first = token_of(client)
    second = token_of(client)
    response = client.post("/op/logout", headers=bearer(first))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    for path in ("/sessions", "/cases"):
        refused = client.get(path, headers=bearer(first))
        assert refused.status_code == 401, path
        assert refused.json()["detail"] == "revoked_session_token"
    assert client.post("/op/logout", headers=bearer(first)).status_code == 401
    assert client.get("/sessions", headers=bearer(second)).status_code == 200
    revocations = [row for row in operator_audits(db) if row.action == models.AUDIT_TOKEN_REVOKED]
    assert [(row.token_hash, row.details) for row in revocations] == [
        (models.hash_token(first), {"reason": "operator_logout"})
    ]


def test_static_token_survives_logout(client):
    assert client.post("/op/logout", headers=STATIC).status_code == 200
    assert client.get("/sessions", headers=STATIC).status_code == 200


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: token[:-2] + ("AA" if not token.endswith("AA") else "BB"),
        lambda token: token.split(".")[0] + ".",
        lambda token: "not-a-token",
    ],
)
def test_tampered_tokens_are_invalid_operator_token(client, mutate):
    response = client.get("/sessions", headers=bearer(mutate(token_of(client))))
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_operator_token"


def test_token_signed_with_another_secret_is_refused(client, monkeypatch):
    monkeypatch.setenv(websocket.WS_TOKEN_SECRET_ENV, "some-other-secret")
    foreign, _ = auth.create_operator_token(USERNAME)
    monkeypatch.undo()
    monkeypatch.setenv(auth.ADMIN_USERNAME_ENV, USERNAME)
    monkeypatch.setenv(auth.ADMIN_PASSWORD_HASH_ENV, PASSWORD_HASH)
    response = client.get("/sessions", headers=bearer(foreign))
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_operator_token"


def test_live_session_token_is_not_an_operator_token(client):
    created = client.post("/sessions", json={"session_id": "S-OP", "case_id": "CASE001"}, headers=STATIC)
    joined = client.post(
        "/sessions/S-OP/join",
        json={"join_code": created.json()["join_codes"]["wizard"], "client_id": "w1"},
    )
    wizard = joined.json()["access_token"]
    response = client.get("/sessions", headers=bearer(wizard))
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_operator_token"


def test_password_hash_format():
    encoded = admin_password.hash_password("pw-1234567890")
    assert "$" not in encoded
    assert encoded.startswith("scrypt:16384:8:1:")
    assert admin_password.verify_password("pw-1234567890", encoded)
    assert not admin_password.verify_password("pw-1234567891", encoded)
    assert admin_password.hash_password("x", salt=b"s" * 16) == admin_password.hash_password(
        "x", salt=b"s" * 16
    )
    for broken in (
        "",
        "scrypt:16384:8:1:abc",
        "bcrypt:16384:8:1:c2FsdHNhbHQ:ZGlnZXN0ZGlnZXN0ZGlnZXN0",
        "scrypt:1000:8:1:c2FsdHNhbHQ:ZGlnZXN0ZGlnZXN0ZGlnZXN0",  # n not a power of two
        "scrypt:16384:8:1:!!:ZGlnZXN0",
        encoded.replace(":16384:", f":{2**20}:"),  # above the cap
    ):
        assert not admin_password.is_valid_hash(broken), broken
        assert not admin_password.verify_password("x", broken)
