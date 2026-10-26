"""Teacher REST API tests (spec §3.2 D, §10.1, §12; ADR-0004 §3, §4).

Covers the Server/API & Auth Agent slice -- `app/auth.py` plus the four review
endpoints added to `app/main.py`:

  - `app/auth.py`: token verification pinned against the WebSocket verifier it
    mirrors, the session-id binding that stops a token for session A reading
    session B, and the revocation rules including the post-session exemption
    that keeps review reachable after `POST /sessions/{id}/end`;
  - `GET /cases/{case_id}/rubric`: spec §12/§13 scoring criteria, and the
    containment check on a URL-supplied path segment;
  - `GET /sessions/{id}/recording[/status]`: mp4-preferred / raw-WebM fallback
    (ADR-0004 §4) and the access audit row (ADR-0004 §3);
  - `GET /sessions/{id}/timeline`: t=0 derivation, clip-span pairing and
    manifest enrichment, Wizard annotations, and -- the regression this suite
    exists for -- that the server-monotonic `start_at_ms` never leaks into the
    wall-clock relative times;
  - `GET /sessions/{id}/audit`: ordering and token masking.

Every test runs against its own in-memory SQLite engine (injected through the
`get_db` dependency) and its own scratch recording root, so nothing here
touches `sessions/mind-probe.sqlite` or the real `server/sessions/`.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Set before app.main imports app.websocket, which reads it when signing.
# setdefault so a secret an earlier test module set is never clobbered.
os.environ.setdefault("MINDPROBE_WS_TOKEN_SECRET", "test-api-secret")

from app import auth, cases, models, recording, websocket, storage
from app.events import EventEnvelope, EventType, Source
from app.main import app
from app.models import Base

#: A minimal but genuine WebM/EBML header, matching `test_recording.py`, so a
#: planted chunk passes `validate_header_chunk` the way a real one would.
EBML_HEADER = recording.EBML_MAGIC + b"\x01\x00\x00\x00\x00\x00\x00\x23header"

#: Wall-clock milliseconds, the domain `Event.server_timestamp_ms` lives in
#: (`websocket._now_ms()`). Fixed so the timeline assertions are exact.
T0_WALL_MS = 1_760_000_000_000

CASE_ID = "CASE001"
CLIP_ID = "C001_TIMELINE_LEAVE_02"
FALLBACK_CLIP_ID = "C001_FALLBACK_CLARIFY_01"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def db_factory():
    """A sessionmaker bound to a private in-memory database.

    StaticPool keeps every session on one connection, so the `:memory:`
    database is shared with the thread TestClient runs handlers on.
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
    """TestClient with `get_db` pointed at the test database.

    Deliberately not entered as a context manager: the lifespan would run
    `init_db()` against the real engine (same reasoning as
    `test_recording.py`'s client fixture).
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


@pytest.fixture()
def sessions_root(monkeypatch):
    """A throwaway recording root, pointed at by `recording.SESSIONS_DIR`.

    Deliberately not pytest's `tmp_path`, matching `test_recording.py` and
    `test_m1_e2e.py`: that lives under the system temp directory, which is not
    always writable in the environments this suite runs in.
    """
    root = Path(__file__).resolve().parent / ".tmp_api" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(recording, "SESSIONS_DIR", root)
    monkeypatch.setenv("MINDPROBE_STORAGE_BACKEND", "local")
    monkeypatch.setenv("MINDPROBE_LOCAL_STORAGE_DIR", str(root / "objects"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def scratch_dir():
    """A writable throwaway directory next to the suite.

    Same reasoning as `sessions_root`: pytest's `tmp_path` lives under the
    system temp directory, which is not always writable here.
    """
    root = Path(__file__).resolve().parent / ".tmp_api" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


# --- Helpers ----------------------------------------------------------------


def create_session(client: TestClient, session_id: str, case_id: str = CASE_ID) -> dict:
    response = client.post(
        "/sessions", json={"session_id": session_id, "case_id": case_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


def join(client: TestClient, session_id: str, role: str, join_codes: dict) -> str:
    """Redeem a join code and return the raw token it set as a cookie.

    The client's cookie jar is cleared afterwards so each request in a test
    states which identity it is using, rather than inheriting whichever role
    joined last.
    """
    response = client.post(
        f"/sessions/{session_id}/join", json={"join_code": join_codes[role]}
    )
    assert response.status_code == 200, response.text
    token = response.cookies[websocket.WS_SESSION_COOKIE]
    client.cookies.clear()
    return token


def as_cookie(token: str) -> dict:
    """The role-bound session cookie, as an explicit per-request header.

    Sent as a header rather than through `TestClient`'s cookie jar so each
    request states its own identity -- a shared jar would silently carry the
    last role joined into the next assertion. The server still reads it through
    `request.cookies`, so this is the real browser path, not a bypass.
    """
    return {"Cookie": f"{websocket.WS_SESSION_COOKIE}={token}"}


def as_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# Matches tests/conftest.py's MINDPROBE_OPERATOR_TOKEN.
OP_HEADERS = as_bearer("test-operator-token-0123456789abcdef")


def add_event(
    db,
    session_id: str,
    *,
    sequence: int,
    event_type: EventType,
    source: Source = Source.WIZARD,
    server_timestamp_ms: int | None = None,
    payload: dict | None = None,
) -> models.Event:
    """Persist one event through the real `models.append_event` path."""
    envelope = EventEnvelope(
        session_id=session_id,
        sequence=sequence,
        client_timestamp_ms=sequence * 1000,
        server_timestamp_ms=server_timestamp_ms,
        source=source,
        event_type=event_type,
        payload=payload or {},
    )
    event, _is_new = models.append_event(db, session_id, envelope)
    return event


def write_chunk(root: Path, session_id: str, sequence: int, data: bytes) -> Path:
    path = root / session_id / "chunks" / f"{sequence:06d}.webm"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def fake_websocket(token: str | None) -> SimpleNamespace:
    """The minimum `websocket._verify_session_token` reads off a connection."""
    headers = {} if token is None else {"cookie": f"{websocket.WS_SESSION_COOKIE}={token}"}
    return SimpleNamespace(headers=headers)


# --- app/auth.py: token verification ----------------------------------------


def test_auth_token_rules_match_the_websocket_verifier():
    """The WebSocket upgrade reports the same reasons as ``auth.verify_token``.

    ``websocket._verify_session_token`` extracts the credential and delegates
    the claims check, so a signature, expiry, session or role failure uses one
    vocabulary on both transports.
    """
    token = websocket.create_session_token("S-AUTH", "teacher", client_id="c1")

    cases_to_check = [
        # (token, session_id, role, expected reason or None for "accepted")
        (token, "S-AUTH", "teacher", None),
        (token, "S-OTHER", "teacher", "session_token_mismatch"),
        (token, "S-AUTH", "student", "role_token_mismatch"),
        (token[:-4] + "AAAA", "S-AUTH", "teacher", "invalid_session_token"),
        ("not-a-token", "S-AUTH", "teacher", "malformed_session_token"),
        (
            websocket.create_session_token(
                "S-AUTH", "teacher", expires_at_s=int(time.time()) - 10
            ),
            "S-AUTH",
            "teacher",
            "expired_session_token",
        ),
    ]

    for candidate, session_id, role, expected in cases_to_check:
        # REST side.
        if expected is None:
            claims = auth.decode_session_token(
                candidate, session_id=session_id, role=role
            )
            assert claims["session_id"] == session_id
            assert claims["role"] == role
            assert claims["sub"] == "c1"
        else:
            with pytest.raises(auth.TokenInvalid) as excinfo:
                auth.decode_session_token(candidate, session_id=session_id, role=role)
            assert excinfo.value.reason == expected

        # WebSocket side -- same verdict, same reason string.
        if expected is None:
            websocket._verify_session_token(
                fake_websocket(candidate), session_id, role
            )
        else:
            with pytest.raises(websocket.WebSocketAuthError) as ws_excinfo:
                websocket._verify_session_token(
                    fake_websocket(candidate), session_id, role
                )
            assert ws_excinfo.value.reason == expected


def test_decode_session_token_skips_unrequested_bindings():
    """Omitting `session_id`/`role` checks signature and expiry only."""
    token = websocket.create_session_token("S-AUTH", "observer")
    claims = auth.decode_session_token(token)
    assert claims["role"] == "observer"
    assert claims["session_id"] == "S-AUTH"


def test_extract_token_prefers_the_bearer_header_and_gates_the_cookie(monkeypatch):
    both = SimpleNamespace(
        cookies={websocket.WS_SESSION_COOKIE: "cookie-token"},
        headers={"authorization": "Bearer header-token"},
    )
    assert auth.extract_token(both) == "header-token"
    cookie_only = SimpleNamespace(
        cookies={websocket.WS_SESSION_COOKIE: "cookie-token"}, headers={}
    )
    assert auth.extract_token(cookie_only) == "cookie-token"
    monkeypatch.setenv(auth.AUTH_COOKIE_ENV, "false")
    assert auth.extract_token(cookie_only) is None
    assert auth.extract_token(
        SimpleNamespace(cookies={}, headers={"authorization": "bearer header-token"})
    ) == "header-token"
    assert auth.extract_token(SimpleNamespace(cookies={}, headers={})) is None
    assert auth.extract_token(
        SimpleNamespace(cookies={}, headers={"authorization": "Basic abc"})
    ) is None


# --- app/auth.py: request-level authorization -------------------------------


def test_review_endpoints_reject_a_request_with_no_token(client, sessions_root):
    created = create_session(client, "S-NOAUTH")
    assert created["status"] == "created"
    for path in ("/timeline", "/audit", "/recording", "/recording/status"):
        response = client.get(f"/sessions/S-NOAUTH{path}")
        assert response.status_code == 401, path
        assert response.json()["detail"] == "missing_session_token"


def test_review_endpoints_reject_a_student_token(client, sessions_root):
    created = create_session(client, "S-ROLE")
    token = join(client, "S-ROLE", "student", created["join_codes"])
    for path in ("/timeline", "/recording/status"):
        response = client.get(f"/sessions/S-ROLE{path}", headers=as_cookie(token))
        assert response.status_code == 403, path
        assert "may not" in response.json()["detail"]
    assert client.get(
        "/sessions/S-ROLE/audit", headers=as_cookie(token)
    ).status_code == 401


def test_a_token_for_another_session_cannot_read_this_one(client, sessions_root):
    """The session binding is the reason a teacher token is not a master key."""
    first = create_session(client, "S-ONE")
    create_session(client, "S-TWO")
    token = join(client, "S-ONE", "teacher", first["join_codes"])

    assert client.get("/sessions/S-ONE/timeline", headers=as_cookie(token)).status_code == 200
    response = client.get("/sessions/S-TWO/timeline", headers=as_cookie(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "session_token_mismatch"


def test_bearer_header_authenticates_a_non_browser_reviewer(client, sessions_root):
    created = create_session(client, "S-BEARER")
    token = join(client, "S-BEARER", "teacher", created["join_codes"])
    response = client.get("/sessions/S-BEARER/timeline", headers=as_bearer(token))
    assert response.status_code == 200
    assert response.json()["session_id"] == "S-BEARER"


def test_observer_may_read_the_review_surface(client, sessions_root):
    """"教師/審查員" -- observer is the reviewer role, so it is allowed too."""
    create_session(client, "S-OBS")
    token = websocket.create_session_token("S-OBS", "observer")
    assert client.get("/sessions/S-OBS/timeline", headers=as_cookie(token)).status_code == 200


def test_a_revoked_token_is_rejected_while_the_session_is_live(client, db, sessions_root):
    """Revocation still bites on a session that has not ended (ADR-0004 §3)."""
    created = create_session(client, "S-REVOKED")
    token = join(client, "S-REVOKED", "teacher", created["join_codes"])
    assert client.get("/sessions/S-REVOKED/timeline", headers=as_cookie(token)).status_code == 200

    models.record_audit(
        db,
        action=models.AUDIT_TOKEN_REVOKED,
        actor_role="server",
        session_id="S-REVOKED",
        token_hash=models.hash_token(token),
        details={"reason": "manual_revocation"},
    )

    response = client.get("/sessions/S-REVOKED/timeline", headers=as_cookie(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "revoked_session_token"


def test_review_survives_ending_the_session(client, db, sessions_root):
    """Review must still work after `POST /end`, which revokes every token.

    `end_session` calls `models.revoke_session_tokens()` on *all* issued
    tokens, the teacher's included, and `POST /join` refuses to mint a
    replacement once the session is ended -- so enforcing revocation
    unconditionally would lock the review surface at exactly the moment review
    begins. `auth.get_current_actor` exempts ended sessions; this is the test
    that pins that behaviour.
    """
    created = create_session(client, "S-ENDED")
    token = join(client, "S-ENDED", "teacher", created["join_codes"])
    write_chunk(sessions_root, "S-ENDED", 0, EBML_HEADER)
    (sessions_root / "S-ENDED" / "student.mp4").write_bytes(b"mp4-bytes")
    prepared_take(db, "S-ENDED", published=True)

    assert client.post("/sessions/S-ENDED/end").status_code == 200
    assert models.is_token_hash_revoked(db, "S-ENDED", models.hash_token(token))

    for path in ("/timeline", "/recording", "/recording/status"):
        response = client.get(f"/sessions/S-ENDED{path}", headers=as_cookie(token))
        assert response.status_code == 200, f"{path}: {response.text}"

    # /audit is strict (ADR-0005 §5): the ended live token no longer reads it.
    assert (
        client.get("/sessions/S-ENDED/audit", headers=as_cookie(token)).status_code
        == 401
    )

    # The post-session access is still distinguishable in the audit trail.
    viewed = [
        entry
        for entry in client.get("/sessions/S-ENDED/audit", headers=OP_HEADERS).json()[
            "entries"
        ]
        if entry["action"] == auth.AUDIT_RECORDING_VIEWED
    ]
    assert viewed and viewed[0]["details"]["post_session_access"] is True


# --- GET /cases/{case_id}/rubric (spec §12, §13) ----------------------------


def test_rubric_returns_the_case001_scoring_criteria(client):
    created = create_session(client, "S-RUBRIC")
    token = join(client, "S-RUBRIC", "teacher", created["join_codes"])
    response = client.get(f"/cases/{CASE_ID}/rubric", headers=as_cookie(token))
    assert response.status_code == 200
    body = response.json()
    assert body["case_id"] == CASE_ID
    rubric = body["rubric"]
    assert rubric["total"] == 100
    # spec §13: nine categories summing to the total.
    assert len(rubric["categories"]) == 9
    assert sum(c["max"] for c in rubric["categories"]) == 100
    assert {c["key"] for c in rubric["categories"]} >= {"opening", "questioning"}


def test_rubric_requires_a_review_token(client):
    """ADR-0005 §5: teacher/observer/operator only, no longer public."""
    assert client.get(f"/cases/{CASE_ID}/rubric").status_code == 401
    created = create_session(client, "S-RUBRIC2")
    token = join(client, "S-RUBRIC2", "student", created["join_codes"])
    assert (
        client.get(f"/cases/{CASE_ID}/rubric", headers=as_cookie(token)).status_code
        == 403
    )


def test_rubric_404_for_an_unknown_case(client):
    response = client.get("/cases/NO_SUCH_CASE/rubric", headers=OP_HEADERS)
    assert response.status_code == 404
    assert "NO_SUCH_CASE" in response.json()["detail"]


def test_rubric_path_cannot_escape_the_cases_directory():
    """`case_id` is a URL-supplied path segment, so containment is checked."""
    from app.main import _case_file_path

    assert _case_file_path(CASE_ID, "rubric.json") is not None
    assert _case_file_path("../..", "rubric.json") is None
    assert _case_file_path("../../server/app", "main.py") is None


def test_rubric_500_when_the_file_is_unparseable(client, monkeypatch, scratch_dir):
    """A corrupt rubric is a content error, not a missing resource."""
    broken_root = scratch_dir / "cases"
    (broken_root / "BROKEN").mkdir(parents=True)
    (broken_root / "BROKEN" / "rubric.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cases, "CASES_DIR", broken_root)

    response = client.get("/cases/BROKEN/rubric", headers=OP_HEADERS)
    assert response.status_code == 500
    assert "unreadable" in response.json()["detail"]


# --- GET /sessions/{id}/recording/status (spec §10.1) -----------------------


def test_recording_status_reports_chunk_inventory_and_gaps(client, sessions_root):
    created = create_session(client, "S-STATUS")
    token = join(client, "S-STATUS", "teacher", created["join_codes"])
    write_chunk(sessions_root, "S-STATUS", 0, EBML_HEADER)
    write_chunk(sessions_root, "S-STATUS", 1, b"chunk-one")
    write_chunk(sessions_root, "S-STATUS", 3, b"chunk-three")

    body = client.get(
        "/sessions/S-STATUS/recording/status", headers=as_cookie(token)
    ).json()
    assert body["chunk_count"] == 3
    assert body["first_sequence"] == 0
    assert body["last_sequence"] == 3
    assert body["missing_sequences"] == [2]
    assert body["header_chunk_ok"] is True
    assert body["mp4"]["exists"] is False
    assert body["raw_webm"]["exists"] is False


def test_recording_status_on_a_session_with_nothing_uploaded(client, sessions_root):
    created = create_session(client, "S-EMPTY")
    token = join(client, "S-EMPTY", "teacher", created["join_codes"])
    body = client.get(
        "/sessions/S-EMPTY/recording/status", headers=as_cookie(token)
    ).json()
    assert body["chunk_count"] == 0
    assert body["missing_sequences"] == []
    assert body["header_chunk_ok"] is False


def test_recording_status_include_chunks_exposes_stored_metadata(
    client, db, sessions_root
):
    created = create_session(client, "S-DETAIL")
    token = join(client, "S-DETAIL", "teacher", created["join_codes"])
    write_chunk(sessions_root, "S-DETAIL", 0, EBML_HEADER)
    models.save_recording_chunk_metadata(
        db, "S-DETAIL", 0, size_bytes=len(EBML_HEADER), checksum="ab" * 32,
        start_ms=0, end_ms=2000,
    )

    body = client.get(
        "/sessions/S-DETAIL/recording/status?include_chunks=true",
        headers=as_cookie(token),
    ).json()
    assert body["metadata_rows"] == 1
    assert body["chunks"][0]["start_ms"] == 0
    assert body["chunks"][0]["end_ms"] == 2000


def test_recording_status_rejects_an_unsafe_session_id(client, sessions_root):
    """A session id that cannot be one filesystem segment is a 400, not a 500."""
    token = websocket.create_session_token("a*b", "teacher")
    response = client.get("/sessions/a*b/recording/status", headers=as_cookie(token))
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_session_id"


# --- GET /sessions/{id}/recording (spec §10.1; ADR-0004 §3, §4) -------------


def prepared_take(db, session_id, *, published=False):
    rec = models.ensure_recording(db, session_id)
    rec.recording_state = "uploaded" if published else "complete"
    rec.finalize_state, rec.final_sequence, rec.total_chunks = "complete", 0, 1
    if published:
        key = storage.recording_key(session_id)
        path = recording.mp4_path(session_id)
        storage.get_storage().put_file(key, path, storage.file_sha256(path))
        rec.recording_key = key
    db.commit()


def test_recording_returns_signed_mp4(client, sessions_root, db):
    created = create_session(client, "S-MP4")
    token = join(client, "S-MP4", "teacher", created["join_codes"])
    (sessions_root / "S-MP4").mkdir(parents=True)
    (sessions_root / "S-MP4" / "student.mp4").write_bytes(b"mp4-bytes")
    (sessions_root / "S-MP4" / "student_raw.webm").write_bytes(b"webm-bytes")
    prepared_take(db, "S-MP4", published=True)

    response = client.get("/sessions/S-MP4/recording", headers=as_cookie(token))
    assert response.status_code == 200
    assert response.json()["expires_in"] == 900
    media = client.get(response.json()["url"])
    assert media.headers["content-type"] == "video/mp4"
    assert media.content == b"mp4-bytes"


def test_recording_queues_instead_of_streaming_raw_webm(client, sessions_root, db):
    """ADR-0004 §4: the raw WebM outranks the mp4, so a missing transcode is
    served around rather than reported as an absent recording."""
    created = create_session(client, "S-WEBM")
    token = join(client, "S-WEBM", "teacher", created["join_codes"])
    (sessions_root / "S-WEBM").mkdir(parents=True)
    (sessions_root / "S-WEBM" / "student_raw.webm").write_bytes(b"webm-bytes")
    prepared_take(db, "S-WEBM")

    response = client.get("/sessions/S-WEBM/recording", headers=as_cookie(token))
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert (sessions_root / "S-WEBM" / "student_raw.webm").read_bytes() == b"webm-bytes"


def test_recording_ignores_a_zero_byte_mp4(client, sessions_root, db):
    """An empty mp4 is not a playable artefact; the WebM behind it still is."""
    created = create_session(client, "S-ZERO")
    token = join(client, "S-ZERO", "teacher", created["join_codes"])
    (sessions_root / "S-ZERO").mkdir(parents=True)
    (sessions_root / "S-ZERO" / "student.mp4").write_bytes(b"")
    (sessions_root / "S-ZERO" / "student_raw.webm").write_bytes(b"webm-bytes")
    prepared_take(db, "S-ZERO")

    response = client.get("/sessions/S-ZERO/recording", headers=as_cookie(token))
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_recording_404_when_nothing_has_been_processed(client, sessions_root):
    created = create_session(client, "S-NONE")
    token = join(client, "S-NONE", "teacher", created["join_codes"])
    response = client.get("/sessions/S-NONE/recording", headers=as_cookie(token))
    assert response.status_code == 404
    assert "S-NONE" in response.json()["detail"]


def test_recording_access_is_audited(client, sessions_root, db):
    """ADR-0004 §3: 誰檢視或下載錄影."""
    created = create_session(client, "S-AUDITED")
    token = join(client, "S-AUDITED", "teacher", created["join_codes"])
    (sessions_root / "S-AUDITED").mkdir(parents=True)
    (sessions_root / "S-AUDITED" / "student.mp4").write_bytes(b"mp4-bytes")
    prepared_take(db, "S-AUDITED", published=True)

    assert client.get(
        "/sessions/S-AUDITED/recording", headers=as_cookie(token)
    ).status_code == 200

    entries = client.get(
        "/sessions/S-AUDITED/audit", headers=OP_HEADERS
    ).json()["entries"]
    viewed = [e for e in entries if e["action"] == auth.AUDIT_RECORDING_VIEWED]
    assert len(viewed) == 1
    assert viewed[0]["actor_role"] == "teacher"
    assert viewed[0]["details"]["artefact"] == "mp4"
    assert "url" not in viewed[0]["details"]
    # The digest identifies the actor; the token itself never appears.
    assert viewed[0]["token_hash_prefix"] == models.hash_token(token)[:8]


def test_a_404_recording_is_not_audited(client, sessions_root):
    """Nothing was disclosed, so there is nothing to record as disclosed."""
    created = create_session(client, "S-NOAUDIT")
    token = join(client, "S-NOAUDIT", "teacher", created["join_codes"])
    assert client.get(
        "/sessions/S-NOAUDIT/recording", headers=as_cookie(token)
    ).status_code == 404

    entries = client.get(
        "/sessions/S-NOAUDIT/audit", headers=OP_HEADERS
    ).json()["entries"]
    assert [e for e in entries if e["action"] == auth.AUDIT_RECORDING_VIEWED] == []


# --- GET /sessions/{id}/timeline (spec §3.2 D, §9) --------------------------


def seed_timeline(db, session_id: str) -> None:
    """A representative session: one clip, a fallback, a pause and a note."""
    add_event(
        db, session_id, sequence=1, source=Source.SERVER,
        event_type=EventType.SESSION_CREATED, server_timestamp_ms=T0_WALL_MS - 3000,
    )
    add_event(
        db, session_id, sequence=2, source=Source.SERVER,
        event_type=EventType.SESSION_STARTED, server_timestamp_ms=T0_WALL_MS,
    )
    add_event(
        db, session_id, sequence=3, event_type=EventType.CLIP_COMMAND,
        server_timestamp_ms=T0_WALL_MS + 1000,
        payload={"clip_id": CLIP_ID, "command_id": "cmd-1"},
    )
    add_event(
        db, session_id, sequence=4, source=Source.STUDENT,
        event_type=EventType.CLIP_STARTED, server_timestamp_ms=T0_WALL_MS + 1200,
        payload={"clip_id": CLIP_ID, "event_id": "ev-clip-start"},
    )
    add_event(
        db, session_id, sequence=5, source=Source.STUDENT,
        event_type=EventType.CLIP_ENDED, server_timestamp_ms=T0_WALL_MS + 5580,
        payload={
            "clip_id": CLIP_ID,
            "played_duration_ms": 4380,
            "event_id": "ev-clip-end",
        },
    )
    add_event(
        db, session_id, sequence=6, event_type=EventType.FALLBACK_USED,
        server_timestamp_ms=T0_WALL_MS + 7000,
        payload={
            "fallback_category": "CLARIFY",
            "resolved_clip_id": FALLBACK_CLIP_ID,
            "consecutive_fallbacks": 1,
            "event_id": "ev-fallback",
        },
    )
    add_event(
        db, session_id, sequence=7, event_type=EventType.SESSION_PAUSED,
        server_timestamp_ms=T0_WALL_MS + 9000,
        payload={"reason": "network_failure", "command_id": "cmd-pause"},
    )
    add_event(
        db, session_id, sequence=8, event_type=EventType.SESSION_RESUMED,
        server_timestamp_ms=T0_WALL_MS + 12000,
        payload={"event_id": "ev-resume"},
    )
    add_event(
        db, session_id, sequence=9, event_type=EventType.UNCOVERED_QUESTION,
        server_timestamp_ms=T0_WALL_MS + 15000,
        payload={
            "question_text": "你幾點返到屋企？",
            "note": "no approved clip",
            "event_id": "ev-uncovered",
        },
    )


def timeline_of(client, session_id, token) -> dict:
    response = client.get(
        f"/sessions/{session_id}/timeline", headers=as_cookie(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_timeline_is_relative_to_the_session_started_event(client, db, sessions_root):
    created = create_session(client, "S-TL")
    token = join(client, "S-TL", "teacher", created["join_codes"])
    seed_timeline(db, "S-TL")

    body = timeline_of(client, "S-TL", token)
    assert body["t0_source"] == "session_started_event"
    assert body["t0_server_timestamp_ms"] == T0_WALL_MS
    assert body["case_id"] == CASE_ID
    assert body["event_count"] == 9

    by_type = {e["event_type"]: e for e in body["entries"]}
    # Setup precedes t=0 and is reported honestly rather than clamped to it.
    assert by_type["session_created"]["t_ms"] == -3000
    assert by_type["session_started"]["t_ms"] == 0
    assert by_type["uncovered_question"]["t_ms"] == 15000

    # Ordered left to right for the review scrubber.
    times = [e["t_ms"] for e in body["entries"]]
    assert times == sorted(times)


def test_timeline_pairs_clip_events_into_spans_with_manifest_text(
    client, db, sessions_root
):
    """spec §3.2 D / §12: 台詞文字與對應案情類別 alongside the playback span."""
    created = create_session(client, "S-CLIP")
    token = join(client, "S-CLIP", "teacher", created["join_codes"])
    seed_timeline(db, "S-CLIP")

    body = timeline_of(client, "S-CLIP", token)
    clips = [e for e in body["entries"] if e["kind"] == "clip"]
    assert len(clips) == 1

    span = clips[0]
    assert span["clip_id"] == CLIP_ID
    assert span["started"] is True
    assert span["t_ms"] == 1200
    assert span["end_t_ms"] == 5580
    assert span["end_event_type"] == "clip_ended"
    assert span["duration_ms"] == 4380
    assert span["played_duration_ms"] == 4380
    assert span["clip"]["text"] == "我已經說了，我大約九時左右離開。"
    assert span["clip"]["category"] == "timeline"
    assert span["clip"]["intent"] == "ask_leave_time"
    assert span["clip"]["manifest_duration_ms"] == 4380


def test_timeline_surfaces_wizard_actions(client, db, sessions_root):
    created = create_session(client, "S-WIZ")
    token = join(client, "S-WIZ", "teacher", created["join_codes"])
    seed_timeline(db, "S-WIZ")

    actions = {
        e["action"]: e
        for e in timeline_of(client, "S-WIZ", token)["entries"]
        if e["kind"] == "wizard_action"
    }
    assert actions["session_paused"]["reason"] == "network_failure"
    assert actions["fallback_used"]["fallback_category"] == "CLARIFY"
    assert actions["fallback_used"]["resolved_clip_id"] == FALLBACK_CLIP_ID
    assert actions["fallback_used"]["consecutive_fallbacks"] == 1
    assert actions["uncovered_question"]["question_text"] == "你幾點返到屋企？"
    assert actions["uncovered_question"]["note"] == "no approved clip"
    assert actions["session_resumed"]["t_ms"] == 12000
    # A clip_command is a Wizard action, enriched from the same manifest.
    assert actions["clip_command"]["clip"]["intent"] == "ask_leave_time"


def test_timeline_never_mixes_the_monotonic_start_at_ms_into_relative_times(
    client, db, sessions_root
):
    """The regression this endpoint's design turns on (ADR-0003).

    `Session.start_at_ms` is server *monotonic* (`websocket._monotonic_ms() +
    2000` -- process uptime), while `Event.server_timestamp_ms` is server
    *wall* time. Subtracting one from the other yields offsets around 1.7e12
    ms. t=0 therefore comes from the `session_started` event, and
    `start_at_ms` is reported separately under a name that says which clock it
    is on.
    """
    created = create_session(client, "S-CLOCK")
    token = join(client, "S-CLOCK", "teacher", created["join_codes"])
    models.start_session(db, "S-CLOCK", start_at_ms=123_456)
    seed_timeline(db, "S-CLOCK")

    body = timeline_of(client, "S-CLOCK", token)
    assert body["scheduled_start_at_monotonic_ms"] == 123_456
    assert body["t0_server_timestamp_ms"] == T0_WALL_MS
    # Every relative time is an interview-scale offset, not an epoch.
    assert all(abs(e["t_ms"]) < 60 * 60 * 1000 for e in body["entries"])


def test_timeline_pairs_a_clip_replayed_later_against_its_own_start(
    client, db, sessions_root
):
    created = create_session(client, "S-REPLAY")
    token = join(client, "S-REPLAY", "teacher", created["join_codes"])
    for index, (sequence, event_type, offset) in enumerate(
        [
            (1, EventType.CLIP_STARTED, 0),
            (2, EventType.CLIP_ENDED, 1000),
            (3, EventType.CLIP_STARTED, 20_000),
            (4, EventType.CLIP_ENDED, 21_000),
        ]
    ):
        add_event(
            db, "S-REPLAY", sequence=sequence, source=Source.STUDENT,
            event_type=event_type, server_timestamp_ms=T0_WALL_MS + offset,
            payload={"clip_id": CLIP_ID, "event_id": f"ev-{index}"},
        )

    spans = [
        e
        for e in timeline_of(client, "S-REPLAY", token)["entries"]
        if e["kind"] == "clip"
    ]
    assert [(s["t_ms"], s["end_t_ms"]) for s in spans] == [(0, 1000), (20_000, 21_000)]


def test_timeline_keeps_a_clip_end_that_has_no_matching_start(
    client, db, sessions_root
):
    """The event log is the record; half a pair is still something that happened."""
    created = create_session(client, "S-ORPHAN")
    token = join(client, "S-ORPHAN", "teacher", created["join_codes"])
    add_event(
        db, "S-ORPHAN", sequence=1, source=Source.STUDENT,
        event_type=EventType.CLIP_INTERRUPTED, server_timestamp_ms=T0_WALL_MS,
        payload={
            "clip_id": CLIP_ID,
            "played_duration_ms": 2750,
            "reason": "student_barge_in",
        },
    )

    clips = [
        e
        for e in timeline_of(client, "S-ORPHAN", token)["entries"]
        if e["kind"] == "clip"
    ]
    assert len(clips) == 1
    assert clips[0]["started"] is False
    assert clips[0]["end_event_type"] == "clip_interrupted"
    assert clips[0]["played_duration_ms"] == 2750


def test_timeline_records_why_a_clip_was_interrupted(client, db, sessions_root):
    """spec §7.2: the barge-in reason is the point of logging the interruption."""
    created = create_session(client, "S-BARGE")
    token = join(client, "S-BARGE", "teacher", created["join_codes"])
    add_event(
        db, "S-BARGE", sequence=1, source=Source.STUDENT,
        event_type=EventType.CLIP_STARTED, server_timestamp_ms=T0_WALL_MS,
        payload={"clip_id": CLIP_ID, "event_id": "ev-a"},
    )
    add_event(
        db, "S-BARGE", sequence=2, source=Source.STUDENT,
        event_type=EventType.CLIP_INTERRUPTED, server_timestamp_ms=T0_WALL_MS + 2750,
        payload={
            "clip_id": CLIP_ID,
            "played_duration_ms": 2750,
            "reason": "student_barge_in",
            "event_id": "ev-b",
        },
    )

    span = [
        e
        for e in timeline_of(client, "S-BARGE", token)["entries"]
        if e["kind"] == "clip"
    ][0]
    assert span["interrupt_reason"] == "student_barge_in"
    assert span["duration_ms"] == 2750


def test_timeline_reports_recording_spans_on_their_own_time_base(
    client, db, sessions_root
):
    """Chunk intervals are the browser's offsets, so they stay out of `entries`."""
    created = create_session(client, "S-SPANS")
    token = join(client, "S-SPANS", "teacher", created["join_codes"])
    for sequence in (0, 1, 2, 5, 6):
        models.save_recording_chunk_metadata(
            db, "S-SPANS", sequence, size_bytes=100,
            start_ms=sequence * 2000, end_ms=(sequence + 1) * 2000,
        )

    body = timeline_of(client, "S-SPANS", token)
    spans = body["recording_spans"]
    assert [(s["first_sequence"], s["last_sequence"]) for s in spans] == [(0, 2), (5, 6)]
    assert spans[0]["chunk_count"] == 3
    assert spans[0]["start_ms"] == 0
    assert spans[0]["end_ms"] == 6000
    assert spans[0]["size_bytes"] == 300
    assert all(s["time_base"] == "client_relative_to_session_start" for s in spans)
    # Not interleaved into the wall-clock timeline.
    assert all(e["kind"] != "recording_span" for e in body["entries"])
    assert body["summary"]["recording_span_count"] == 2


def test_timeline_summary_counts_what_the_review_header_shows(
    client, db, sessions_root
):
    created = create_session(client, "S-SUM")
    token = join(client, "S-SUM", "teacher", created["join_codes"])
    seed_timeline(db, "S-SUM")

    summary = timeline_of(client, "S-SUM", token)["summary"]
    assert summary["clip_count"] == 1
    assert summary["entries_by_kind"]["wizard_action"] == 5
    assert summary["entries_by_kind"]["lifecycle"] == 2
    assert summary["wizard_actions_by_type"]["fallback_used"] == 1
    assert summary["first_t_ms"] == -3000
    assert summary["last_t_ms"] == 15000
    # 9 events, two of which fold into a single clip span.
    assert summary["entry_count"] == 8


def test_timeline_on_a_session_with_no_events(client, sessions_root):
    created = create_session(client, "S-BARE")
    token = join(client, "S-BARE", "teacher", created["join_codes"])
    body = timeline_of(client, "S-BARE", token)
    assert body["t0_server_timestamp_ms"] is None
    assert body["t0_source"] == "none"
    assert body["entries"] == []
    assert body["recording_spans"] == []
    assert body["summary"]["entry_count"] == 0


def test_timeline_falls_back_to_the_earliest_event_without_a_start(
    client, db, sessions_root
):
    """`min`, not "lowest sequence": each source numbers its own events."""
    created = create_session(client, "S-NOSTART")
    token = join(client, "S-NOSTART", "teacher", created["join_codes"])
    add_event(
        db, "S-NOSTART", sequence=2, event_type=EventType.CLIP_COMMAND,
        server_timestamp_ms=T0_WALL_MS + 500, payload={"clip_id": CLIP_ID},
    )
    add_event(
        db, "S-NOSTART", sequence=1, source=Source.SERVER,
        event_type=EventType.OBSERVER_NOTE, server_timestamp_ms=T0_WALL_MS,
        payload={"note": "setup"},
    )

    body = timeline_of(client, "S-NOSTART", token)
    assert body["t0_source"] == "earliest_event"
    assert body["t0_server_timestamp_ms"] == T0_WALL_MS
    assert [e["t_ms"] for e in body["entries"]] == [0, 500]


def test_timeline_degrades_when_the_clip_manifest_is_unreadable(
    client, db, sessions_root
):
    """A missing manifest costs subtitle text, not the whole timeline (spec §12)."""
    created = create_session(client, "S-NOMANIFEST", case_id="NO_SUCH_CASE")
    token = join(client, "S-NOMANIFEST", "teacher", created["join_codes"])
    seed_timeline(db, "S-NOMANIFEST")

    body = timeline_of(client, "S-NOMANIFEST", token)
    span = [e for e in body["entries"] if e["kind"] == "clip"][0]
    assert span["clip_id"] == CLIP_ID
    assert span["clip"] is None
    assert span["duration_ms"] == 4380


def test_timeline_404_for_an_unknown_session(client, sessions_root):
    token = websocket.create_session_token("S-GHOST", "teacher")
    response = client.get("/sessions/S-GHOST/timeline", headers=as_cookie(token))
    assert response.status_code == 404
    assert "S-GHOST" in response.json()["detail"]


# --- GET /sessions/{id}/audit (ADR-0004 §3) ---------------------------------


def test_audit_returns_the_session_trail_in_order(client, db, sessions_root):
    created = create_session(client, "S-AUD")
    token = join(client, "S-AUD", "teacher", created["join_codes"])
    models.start_session(db, "S-AUD")
    models.pause_session(db, "S-AUD", reason="ethical_or_safety_stop", actor_role="wizard")
    models.record_audit(
        db,
        action=models.AUDIT_CONNECTION_TAKEOVER,
        actor_role="student",
        session_id="S-AUD",
    )

    entries = client.get("/sessions/S-AUD/audit", headers=OP_HEADERS).json()[
        "entries"
    ]
    assert [e["action"] for e in entries] == [
        models.AUDIT_TOKEN_ISSUED,
        models.AUDIT_SESSION_PAUSED,
        models.AUDIT_CONNECTION_TAKEOVER,
    ]
    assert [e["id"] for e in entries] == sorted(e["id"] for e in entries)
    paused = entries[1]
    assert paused["actor_role"] == "wizard"
    assert paused["details"]["reason"] == "ethical_or_safety_stop"
    assert paused["created_at"] is not None
    assert paused["token_hash_prefix"] is None


def test_audit_masks_tokens(client, sessions_root):
    """ADR-0004 §3: 日誌不得儲存 token 原文 -- nor return one."""
    created = create_session(client, "S-MASK")
    token = join(client, "S-MASK", "teacher", created["join_codes"])
    (sessions_root / "S-MASK").mkdir(parents=True)
    (sessions_root / "S-MASK" / "student.mp4").write_bytes(b"mp4-bytes")
    client.get("/sessions/S-MASK/recording", headers=as_cookie(token))

    raw = client.get("/sessions/S-MASK/audit", headers=OP_HEADERS).text
    assert token not in raw
    assert models.hash_token(token) not in raw  # not even the full digest
    # Nothing in the response has the `<payload>.<signature>` shape of a token.
    assert models._TOKEN_SHAPE.search(raw) is None

    entries = json.loads(raw)["entries"]
    issued = [e for e in entries if e["action"] == models.AUDIT_TOKEN_ISSUED][0]
    assert issued["token_hash_prefix"] == models.hash_token(token)[:8]
    assert len(issued["token_hash_prefix"]) == 8


def test_audit_outlives_the_session_row(client, db, sessions_root):
    """`AuditLog.session_id` is deliberately not a foreign key."""
    create_session(client, "S-ORPHANED")
    models.record_audit(
        db,
        action=models.AUDIT_SESSION_PAUSED,
        actor_role="wizard",
        session_id="S-ORPHANED",
        details={"reason": "manual"},
    )
    db.delete(models.get_session(db, "S-ORPHANED"))
    db.commit()

    response = client.get("/sessions/S-ORPHANED/audit", headers=OP_HEADERS)
    assert response.status_code == 200
    assert len(response.json()["entries"]) == 1


def test_audit_of_a_session_with_no_rows_is_empty(client, sessions_root):
    create_session(client, "S-QUIET")
    body = client.get("/sessions/S-QUIET/audit", headers=OP_HEADERS).json()
    assert body == {"session_id": "S-QUIET", "entries": []}
