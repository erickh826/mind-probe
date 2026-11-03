"""Upgrade refusals through a real uvicorn server and a real WebSocket client.

TestClient reports a close code even when the server closes before
``accept()``. uvicorn does not: it answers HTTP 403, and a browser sees only
close 1006 with no reason, so the Student and Wizard cannot tell a revoked
token from a busy role and retry forever. Only a real server shows that.
"""

from __future__ import annotations

import socket
import threading
import time
from uuid import uuid4

import pytest
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from app import models
from app.main import app
from app.models import Base
from app.websocket import WS_PROTOCOL, create_session_token


@pytest.fixture(autouse=True)
def db_factory(monkeypatch):
    """A private in-memory database shared with the server thread."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(models, "SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture
def base_url():
    """Serve the app on an ephemeral port; uvicorn picks its own ws protocol."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    yield f"ws://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()


def _session_id() -> str:
    session_id = f"S{uuid4().hex}"
    with models.session_scope() as db:
        models.create_session(db, session_id, "CASE001")
    return session_id


def _open(base_url: str, session_id: str, role: str, token: str):
    return connect(
        f"{base_url}/ws/{session_id}/{role}",
        subprotocols=[WS_PROTOCOL, f"bearer.{token}"],
        open_timeout=5,
    )


def _refusal(base_url: str, session_id: str, role: str, token: str) -> tuple[int, str]:
    """Open like a browser, then read the close the server sends first."""
    with _open(base_url, session_id, role, token) as ws:
        assert ws.subprotocol == WS_PROTOCOL
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=5)
    assert closed.value.rcvd is not None, "closed without a close frame (1006)"
    return closed.value.rcvd.code, closed.value.rcvd.reason


def test_busy_role_reaches_the_browser_as_retryable_4429(base_url):
    session_id = _session_id()
    token = create_session_token(session_id, "student")
    with _open(base_url, session_id, "student", token) as first:
        assert _refusal(base_url, session_id, "student", token) == (
            4429,
            "role_already_connected",
        )
        first.send('{"type": "ping", "ping_id": "still-here", "client_timestamp_ms": 0}')
        assert "still-here" in first.recv(timeout=5)


def test_revoked_token_reaches_the_browser_as_4401_token_revoked(base_url):
    session_id = _session_id()
    token = create_session_token(session_id, "student")
    with models.session_scope() as db:
        models.revoke_token_hash(db, session_id, models.hash_token(token))
    assert _refusal(base_url, session_id, "student", token) == (4401, "token_revoked")


def _broken(*_args, **_kwargs):
    raise SQLAlchemyError("database is locked")


def test_database_failure_reaches_the_browser_as_retryable_1013(base_url, monkeypatch):
    session_id = _session_id()
    token = create_session_token(session_id, "wizard")
    real_get_session = models.get_session

    # Live socket: the frame that hits the outage is closed, not applied.
    with _open(base_url, session_id, "wizard", token) as ws:
        ws.send('{"type": "ping", "ping_id": "admitted", "client_timestamp_ms": 0}')
        assert "admitted" in ws.recv(timeout=5)
        monkeypatch.setattr(models, "get_session", _broken)
        ws.send(
            '{"sequence": 1, "client_timestamp_ms": 0, "event_type": "observer_note",'
            ' "payload": {"event_id": "evt-during-outage", "note": "x"}}'
        )
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=5)
    assert closed.value.rcvd is not None
    assert (closed.value.rcvd.code, closed.value.rcvd.reason) == (
        1013,
        "session_state_unavailable",
    )

    # Upgrade during the outage: still a visible, retryable refusal.
    assert _refusal(base_url, session_id, "wizard", token) == (
        1013,
        "session_state_unavailable",
    )

    # The outage ends: the same token is admitted again.
    monkeypatch.setattr(models, "get_session", real_get_session)
    with _open(base_url, session_id, "wizard", token) as ws:
        ws.send(
            '{"sequence": 2, "client_timestamp_ms": 0, "event_type": "observer_note",'
            ' "payload": {"event_id": "evt-after-outage", "note": "x"}}'
        )
        assert '"accepted"' in ws.recv(timeout=5)
    with models.session_scope() as db:
        stored = [e.payload.get("event_id") for e in models.load_events(db, session_id)]
    assert stored == ["evt-after-outage"]
