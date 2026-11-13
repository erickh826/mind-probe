"""``POST /recording/{id}/claim``: one take per session, decided before capture (D3).

A device that has never seen this session's recording must not start a second
take. The first chunk is too late to tell: a recorder may have started, or its
first slice may still be uploading. So the tab claims the take after the camera
is granted and before ``MediaRecorder.start()``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, recording, websocket
from app.main import app
from app.models import Base
from app.websocket import WS_PROTOCOL, create_session_token

OPERATOR = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}


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
def client(db_factory, monkeypatch, tmp_path):
    def _override_get_db():
        session = db_factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path)
    app.dependency_overrides[models.get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(models.get_db, None)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _with_db(fn):
    gen = app.dependency_overrides[models.get_db]()
    db = next(gen)
    try:
        return fn(db)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def _open_session(client: TestClient, *, started: bool = True) -> tuple[str, str]:
    """A session and its student's live token.

    ``started`` moves the row to ``active``. A claim is refused until the
    server session is in progress, so the older claim cases opt in.
    """
    session_id = f"S-{uuid4().hex[:8]}"
    created = client.post(
        "/sessions",
        json={"session_id": session_id, "case_id": "CASE001"},
        headers=OPERATOR,
    )
    assert created.status_code == 201, created.text
    joined = client.post(
        f"/sessions/{session_id}/join",
        json={"join_code": created.json()["join_codes"]["student"], "client_id": "student-test"},
    )
    assert joined.status_code == 200, joined.text
    client.cookies.clear()
    if started:
        _with_db(lambda db: models.start_session(db, session_id))
    return session_id, joined.json()["access_token"]


def _socket(client: TestClient, session_id: str, token: str):
    return client.websocket_connect(
        f"/ws/{session_id}/student", subprotocols=[WS_PROTOCOL, f"bearer.{token}"]
    )


def _claim(client: TestClient, session_id: str, token: str, take_id: str):
    return client.post(
        f"/recording/{session_id}/claim",
        json={"take_id": take_id},
        headers=_bearer(token),
    )


def _status(client: TestClient, session_id: str, token: str) -> dict:
    response = client.get(f"/recording/{session_id}/status", headers=_bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def _upload(client: TestClient, session_id: str, token: str, sequence: int):
    data = recording.EBML_MAGIC + b"\x01\x00\x00\x00\x00\x00\x00\x23header" + bytes([sequence])
    return client.post(
        f"/recording/{session_id}/chunk",
        data={"sequence": sequence},
        files={"chunk": (f"{sequence:06d}.webm", data, "video/webm")},
        headers=_bearer(token),
    )


def _take(db_factory, session_id: str) -> models.Recording | None:
    with db_factory() as db:
        return models.get_recording(db, session_id)


TAKE_A = "take-aaaaaaaa"
TAKE_B = "take-bbbbbbbb"


def test_claim_is_refused_until_the_server_session_is_in_progress(client, db_factory):
    session_id, token = _open_session(client, started=False)
    with _socket(client, session_id, token):
        refused = _claim(client, session_id, token, TAKE_A)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "session_not_started"
    assert _take(db_factory, session_id) is None
    assert _status(client, session_id, token)["take_claimed"] is False


def test_a_scheduled_start_cannot_be_claimed_until_it_is_due(client, db_factory):
    from app.websocket import _monotonic_ms

    session_id, token = _open_session(client, started=False)
    future = _monotonic_ms() + 60_000
    _with_db(lambda db: models.start_session(db, session_id, start_at_ms=future))
    with _socket(client, session_id, token):
        early = _claim(client, session_id, token, TAKE_A)
    assert early.status_code == 409
    assert early.json()["detail"] == "session_not_started"
    assert _take(db_factory, session_id) is None

    def make_due(db):
        session = models.get_session(db, session_id)
        assert session is not None
        session.start_at_ms = 0
        db.commit()

    _with_db(make_due)
    with _socket(client, session_id, token):
        claimed = _claim(client, session_id, token, TAKE_A)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"

    def status(db):
        session = models.get_session(db, session_id)
        assert session is not None
        return session.status

    assert _with_db(status) == "active"


def test_a_successful_claim_clears_a_stored_recording_failure(client):
    from app import websocket as websocket_module

    session_id, token = _open_session(client)
    notice = {
        "phase": "start",
        "step": "camera",
        "error_name": "NotAllowedError",
        "message": "權限被拒",
    }
    _with_db(lambda db: models.set_recording_failure(db, session_id, notice))
    websocket_module.manager.snapshot_for(session_id).recording_failure = dict(notice)
    with _socket(client, session_id, token):
        claimed = _claim(client, session_id, token, TAKE_A)
    assert claimed.status_code == 200, claimed.text

    def failure(db):
        session = models.get_session(db, session_id)
        assert session is not None
        return session.recording_failure

    assert _with_db(failure) is None
    assert websocket_module.manager.snapshot_for(session_id).recording_failure is None


def test_a_paused_session_can_still_claim_its_one_take(client, db_factory):
    session_id, token = _open_session(client, started=True)

    def pause(db):
        models.pause_session(db, session_id, "manual", actor_role="wizard")

    _with_db(pause)
    with _socket(client, session_id, token):
        claimed = _claim(client, session_id, token, TAKE_A)
    assert claimed.status_code == 200, claimed.text
    assert _take(db_factory, session_id) is not None


def test_claim_reserves_the_take_before_any_chunk(client, db_factory):
    session_id, token = _open_session(client)
    assert _status(client, session_id, token)["take_claimed"] is False
    with _socket(client, session_id, token):
        response = _claim(client, session_id, token, TAKE_A)
    assert response.status_code == 200, response.text
    assert response.json() == {"take_id": TAKE_A, "status": "claimed"}

    status = _status(client, session_id, token)
    assert status["take_claimed"] is True
    # The rest is unchanged for older clients: nothing received, not finalized.
    assert status["received_sequences"] == []
    assert status["finalize_state"] == "none"
    assert status["recording_state"] == "recording"
    take = _take(db_factory, session_id)
    assert take is not None and take.take_id == TAKE_A and take.claimed_at is not None


def test_same_take_id_retry_succeeds_after_a_lost_response(client):
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        assert _claim(client, session_id, token, TAKE_A).status_code == 200
        again = _claim(client, session_id, token, TAKE_A)
    assert again.status_code == 200
    assert again.json()["take_id"] == TAKE_A


def test_a_second_take_id_is_refused_and_the_claim_is_never_released(client, db_factory):
    # Same token, another tab: a duplicated tab carries the same token, so the
    # take id -- not the token -- decides.
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        assert _claim(client, session_id, token, TAKE_A).status_code == 200
        refused = _claim(client, session_id, token, TAKE_B)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "recording_already_claimed"
    assert _take(db_factory, session_id).take_id == TAKE_A


def test_two_claims_racing_for_the_insert_get_one_take(client, db_factory, monkeypatch):
    # The loser read "no row" before the winner committed; its insert hits the
    # primary key and it is judged against the winner's row.
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        assert _claim(client, session_id, token, TAKE_A).status_code == 200
        real_get_recording = models.get_recording
        calls = {"n": 0}

        def stale_first_read(db, sid):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get_recording(db, sid)

        monkeypatch.setattr(models, "get_recording", stale_first_read)
        loser = _claim(client, session_id, token, TAKE_B)
        same = _claim(client, session_id, token, TAKE_A)
    assert loser.status_code == 409
    assert loser.json()["detail"] == "recording_already_claimed"
    assert same.status_code == 200
    monkeypatch.setattr(models, "get_recording", real_get_recording)
    assert _take(db_factory, session_id).take_id == TAKE_A


def test_a_take_started_by_an_older_client_cannot_be_claimed(client):
    # An older Student never claims; its first chunk makes the row.
    session_id, token = _open_session(client)
    assert _upload(client, session_id, token, 0).status_code == 200
    assert _status(client, session_id, token)["take_claimed"] is True
    with _socket(client, session_id, token):
        refused = _claim(client, session_id, token, TAKE_A)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "recording_already_claimed"


def test_an_older_clients_chunk_after_a_claim_is_still_stored(client):
    # Backend-first: the chunk path does not know take ids. Full D3 protection
    # needs the new Student; this pins the weaker legacy behaviour.
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        assert _claim(client, session_id, token, TAKE_A).status_code == 200
    assert _upload(client, session_id, token, 0).status_code == 200
    assert _status(client, session_id, token)["received_sequences"] == [0]


def test_same_take_id_is_refused_once_the_take_has_data(client):
    # A pending claim only skips the "already claimed" check for its own id;
    # it never restarts a take that has chunks or a finalize.
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        assert _claim(client, session_id, token, TAKE_A).status_code == 200
        assert _upload(client, session_id, token, 0).status_code == 200
        with_chunk = _claim(client, session_id, token, TAKE_A)
    assert with_chunk.status_code == 409
    assert with_chunk.json()["detail"] == "recording_already_claimed"

    other_id, other_token = _open_session(client)
    with _socket(client, other_id, other_token):
        assert _claim(client, other_id, other_token, TAKE_A).status_code == 200
        finalized = client.post(
            f"/recording/{other_id}/finalize",
            json={"final_sequence": -1, "total_chunks": 0, "total_duration_ms": 0,
                  "reason": "recorder_lost"},
            headers=_bearer(other_token),
        )
        assert finalized.status_code == 200, finalized.text
        after_finalize = _claim(client, other_id, other_token, TAKE_A)
    assert after_finalize.status_code == 409
    assert after_finalize.json()["detail"] == "recording_already_claimed"


def test_claim_needs_the_admitted_student_socket(client, db_factory):
    session_id, token = _open_session(client)
    refused = _claim(client, session_id, token, TAKE_A)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "role_not_admitted"
    # Nothing was written: the take is still free.
    assert _take(db_factory, session_id) is None
    assert _status(client, session_id, token)["take_claimed"] is False


def test_a_replaced_device_cannot_claim_but_the_new_holder_can(client, monkeypatch):
    session_id, old_token = _open_session(client)
    new_token = create_session_token(session_id, "student")
    with _socket(client, session_id, old_token):
        # The old device went quiet past the stale threshold.
        for info in websocket.manager._connections[session_id]["student"]:
            info.last_heartbeat_ms -= websocket.HEARTBEAT_STALE_MS + 1_000
        with _socket(client, session_id, new_token):
            replaced = _claim(client, session_id, old_token, TAKE_A)
            holder = _claim(client, session_id, new_token, TAKE_B)
    assert replaced.status_code == 409
    assert replaced.json()["detail"] == "role_not_admitted"
    assert holder.status_code == 200


def test_ended_session_is_409_session_ended_even_for_a_pending_id(client):
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        assert _claim(client, session_id, token, TAKE_A).status_code == 200
    ended = client.post(f"/sessions/{session_id}/end", headers=OPERATOR)
    assert ended.status_code == 200, ended.text
    for take_id in (TAKE_A, TAKE_B):
        response = _claim(client, session_id, token, take_id)
        assert response.status_code == 409
        assert response.json()["detail"] == "session_ended"


def test_revoked_token_is_401_revoked_session_token_even_for_a_pending_id(client):
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        assert _claim(client, session_id, token, TAKE_A).status_code == 200
    reissued = client.post(
        f"/sessions/{session_id}/join-codes",
        json={"roles": ["student"], "revoke_live": True},
        headers=OPERATOR,
    )
    assert reissued.status_code == 200, reissued.text
    response = _claim(client, session_id, token, TAKE_A)
    assert response.status_code == 401
    assert response.json()["detail"] == "revoked_session_token"


@pytest.mark.parametrize("take_id", ["short", "has space in it", "x" * 65, "日本語のテイクID"])
def test_take_id_must_be_a_plain_short_identifier(client, take_id):
    session_id, token = _open_session(client)
    with _socket(client, session_id, token):
        response = _claim(client, session_id, token, take_id)
    assert response.status_code == 422
