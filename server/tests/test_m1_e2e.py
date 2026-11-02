"""CASE001 M1 end-to-end integration tests (ADR-0004 M1 pass criteria).

Where `test_websocket.py` covers one protocol rule at a time and
`test_persistence.py` covers the REST/storage layer, this module drives the
*whole* M1 pipeline the way the three apps actually use it:

    POST /sessions -> POST /sessions/{id}/join -> WebSocket upgrade with the
    issued cookie -> clock sync -> scheduled start -> Wizard clip commands ->
    fallback -> disconnect/auto-pause -> reconnect_hello -> REST replay.

Each test therefore exercises REST, the realtime layer, and SQLite together
against one database, rather than stubbing any of the three.

The gate is the one sentence under 「M1 通過條件」 in
`docs/adr/0004-cross-cutting-rules.md`. Its six clauses map onto the tests as:

  1. Wizard 可控制 CASE001 影片
     -> test_full_multi_role_session_reaches_case001_playback
  2. Student 穩定播放並錄影 (offset calculation, playback, recording)
     -> test_student_clock_sync_completes_and_offset_is_applied_to_events
        test_events_and_recording_chunks_are_backfilled_after_a_brief_reconnect
  3. 斷線重連後不會重播過期命令
     -> test_expired_and_stale_commands_never_reach_the_student
        test_critical_disconnect_auto_pauses_and_reconnect_restores_state
  4. 事件與錄影 chunk 可以補送
     -> test_events_and_recording_chunks_are_backfilled_after_a_brief_reconnect
  5. 技術斷線會被正確暫停及記錄
     -> test_critical_disconnect_auto_pauses_and_reconnect_restores_state
        test_wizard_disconnect_pauses_without_any_further_inbound_traffic
        test_student_takeover_is_audited_without_pausing_the_live_interview
        (the negative case: a reconnect is not a technical failure)
  6. Teacher 端能按統一時間線回看
     -> test_session_replay_and_audit_trail_back_the_teacher_timeline

ADR-0002's fallback rules are not named in that sentence but gate M1 through
ADR-0004's implementation order:

     -> test_fallback_matrix_resolves_every_category_and_alerts_at_three

Every test runs against its own in-memory SQLite database, wired into *both*
access paths (the `get_db` dependency and `models.SessionLocal`) by the autouse
fixture below, so nothing here touches `sessions/mind-probe.sqlite`.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("MINDPROBE_WS_TOKEN_SECRET", "test-m1-e2e-secret")

from app import auth, cases, models, recording, websocket as websocket_module
from app.main import app
from app.models import Base
from app.websocket import (
    DISCONNECT_GRACE_MS,
    FALLBACK_THRESHOLD,
    HEARTBEAT_STALE_MS,
    WS_SESSION_COOKIE,
    compute_offset_median,
)

#: The repo's real case library. `cases.CASES_DIR` defaults to `./cases`, which
#: does not exist relative to `server/` where pytest runs -- without this every
#: fallback would degrade to a generic clip and the CASE001 resolution below
#: would never be exercised.
REPO_CASES_DIR = Path(__file__).resolve().parents[2] / "cases"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def e2e_env(monkeypatch):
    """One in-memory database reachable from every layer, plus the real cases.

    The realtime layer reaches the database through `websocket._db_scope`,
    which prefers a `get_db` dependency override, while this module's read-back
    helpers (and `_pause_for_network_failure`'s own short-lived scopes) go
    through `models.session_scope()` -> `models.SessionLocal`. Both are pointed
    at the *same* factory here: patching only one of them would leave half the
    flow writing to the real `sessions/mind-probe.sqlite` while the assertions
    read an empty test database.

    StaticPool keeps every session on one connection so the `:memory:` database
    is shared with the handler threads TestClient runs sockets on.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(cases, "CASES_DIR", REPO_CASES_DIR)
    app.dependency_overrides[models.get_db] = _override_get_db
    yield factory
    app.dependency_overrides.pop(models.get_db, None)
    engine.dispose()


def _cancel_background_tasks() -> None:
    """Drop grace timers left on the shared connection manager.

    Disconnect schedules ``_delayed_disconnect_pause_check`` for the real 2s
    window. The next test used to inherit that task, and on Windows the shared
    TestClient portal then missed the wakeup for a ``receive_json`` that had
    already started. Cancelling here does not send a client frame.
    """
    for task in list(websocket_module.manager._background_tasks):
        task.cancel()


@pytest.fixture(autouse=True)
def _isolate_realtime_tasks():
    _cancel_background_tasks()
    yield
    _cancel_background_tasks()


@pytest.fixture(autouse=True)
def _bounded_broadcast_receive(monkeypatch):
    """Do not block in ``receive_json`` before the server has queued the frame.

    Starlette delivers server frames through an anyio memory stream on the
    TestClient portal. On the Windows ProactorEventLoop a receive that is
    already waiting is not woken when a later task sends, so ``receive_json``
    never returns (seen at the multi-role playback receives and at
    ``role_connection_replaced``). The frame still lands in the buffer while
    the test thread is outside ``portal.call``. Poll that buffer, then read.
    Nothing is sent, so a test that asserts no later inbound traffic stays
    honest. If the frame never arrives the wait fails at 8s instead of hanging.
    """
    original = WebSocketTestSession.receive_json

    def receive_json(self, mode: str = "text"):
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            incoming = getattr(self, "_send_rx", None)
            if incoming is not None and incoming.statistics().current_buffer_used > 0:
                return original(self, mode)
            time.sleep(0.01)
        raise AssertionError("no websocket frame within 8.0s")

    monkeypatch.setattr(WebSocketTestSession, "receive_json", receive_json)


@pytest.fixture()
def client():
    """TestClient over one portal shared by every socket in the test.

    Deliberately *not* entered as a context manager: the lifespan would run
    `init_db()` against the real engine (same reasoning as
    `test_persistence.py`'s client fixture).

    Without a portal of its own, each ``websocket_connect`` starts a separate
    event loop. ``session_ended`` then posts the close onto the other roles'
    loops with ``call_soon_threadsafe``. A sibling portal that has already
    stopped raises ``RuntimeError: Event loop is closed``. One portal matches
    production, where every socket is accepted on the server loop.
    """
    with anyio.from_thread.start_blocking_portal() as portal:
        test_client = TestClient(app)
        test_client.portal = portal
        yield test_client
        _cancel_background_tasks()


@pytest.fixture()
def chunk_root(monkeypatch):
    """A throwaway recording root, pointed at by `recording.SESSIONS_DIR`.

    Deliberately not pytest's `tmp_path`: that lives under the system temp
    directory, which is not always writable in the environments this suite runs
    in. Keeping the scratch space beside the tests removes that dependency, and
    it is removed again on teardown so nothing accumulates in the repo.

    Without the redirect the route under test would write into the real
    `server/sessions/`.
    """
    root = Path(__file__).resolve().parent / ".tmp_recordings" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(recording, "SESSIONS_DIR", root)
    yield root
    shutil.rmtree(root, ignore_errors=True)
    # Drop the shared parent too once the last test using it has finished, so a
    # suite run leaves the working tree exactly as it found it.
    try:
        root.parent.rmdir()
    except OSError:
        pass  # another test's scratch space is still in there


# --- Session bootstrap helpers ----------------------------------------------


def _new_session_id() -> str:
    """A unique session id per test.

    `websocket.manager` is a process-global whose snapshots, dedup sets and
    disconnect marks are never cleared, so reusing an id across tests would
    leak realtime state between them.
    """
    return f"S{uuid4().hex}"


def _create_session(client: TestClient, session_id: str) -> dict[str, str]:
    """Create a CASE001 session and return its one-time join codes by role.

    `POST /sessions` issues codes for student/wizard/teacher only; the observer
    is not part of that bootstrap set (`models.JOIN_CODE_ROLES`), so its code is
    issued here through the same public helper.
    """
    resp = client.post(
        "/sessions", json={"session_id": session_id, "case_id": "CASE001"}
    )
    assert resp.status_code == 201, resp.text
    join_codes = dict(resp.json()["join_codes"])

    with models.session_scope() as db:
        join_codes.update(models.issue_join_codes(db, session_id, roles=("observer",)))
    return join_codes


def _join(
    client: TestClient, session_id: str, join_code: str, role: str
) -> dict[str, str]:
    """Redeem a join code and return the cookie header for the WS upgrade.

    The client cookie jar is cleared afterwards so each socket presents exactly
    one role's token: TestClient would otherwise keep the last role's cookie and
    send it alongside the header we pass explicitly.
    """
    resp = client.post(f"/sessions/{session_id}/join", json={"join_code": join_code})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == role
    token = resp.cookies[WS_SESSION_COOKIE]
    client.cookies.clear()
    return {"cookie": f"{WS_SESSION_COOKIE}={token}"}


def _join_roles(
    client: TestClient, session_id: str, *roles: str
) -> dict[str, dict[str, str]]:
    join_codes = _create_session(client, session_id)
    return {role: _join(client, session_id, join_codes[role], role) for role in roles}


def _socket(client: TestClient, session_id: str, role: str, headers: dict[str, str]):
    return client.websocket_connect(f"/ws/{session_id}/{role}", headers=headers)


# --- Message builders -------------------------------------------------------


def _far_future_ms() -> int:
    """An `expires_at_ms` well inside the validity window.

    Short-lived command expiry is checked against `websocket._monotonic_ms()`
    (ADR-0003 §1), so a wall-clock deadline here would sit so far in the future
    that the expiry rule could never be observed at all.
    """
    return time.monotonic_ns() // 1_000_000 + 60_000


def _live_revision(session_id: str) -> int:
    """The snapshot revision a well-behaved client would be holding.

    Every accepted state change bumps it (ADR-0004 §1); a real client tracks the
    value from the last snapshot/broadcast it saw.
    """
    return websocket_module.manager.snapshot_for(session_id).snapshot_revision


def _clip_command(
    session_id: str,
    *,
    sequence: int,
    clip_id: str,
    command_id: str,
    expires_at_ms: int | None = None,
    snapshot_revision: int | None = None,
) -> dict:
    """A Wizard `clip_command` envelope (spec §7, ADR-0001 §5)."""
    return {
        "sequence": sequence,
        "client_timestamp_ms": sequence * 1000,
        "event_type": "clip_command",
        "payload": {
            "command_id": command_id,
            "expires_at_ms": (
                _far_future_ms() if expires_at_ms is None else expires_at_ms
            ),
            "snapshot_revision": (
                _live_revision(session_id)
                if snapshot_revision is None
                else snapshot_revision
            ),
            "clip_id": clip_id,
        },
    }


def _fallback_request(
    session_id: str, *, sequence: int, category: str, command_id: str, reason: str
) -> dict:
    """A Wizard `request_fallback` control message (ADR-0002 §2)."""
    return {
        "type": "request_fallback",
        "sequence": sequence,
        "client_timestamp_ms": sequence * 1000,
        "payload": {
            "command_id": command_id,
            "expires_at_ms": _far_future_ms(),
            "snapshot_revision": _live_revision(session_id),
            "fallback_category": category,
            "reason": reason,
        },
    }


def _reconnect_hello(session_id: str, role: str, *, last_snapshot_revision: int) -> dict:
    return {
        "type": "reconnect_hello",
        "session_id": session_id,
        "role": role,
        "last_snapshot_revision": last_snapshot_revision,
        "last_acked_sequence": 0,
        "pending_event_ids": [],
    }


def _start_session(wizard, *, sequence: int = 1) -> dict:
    """Drive an accepted `session_started` and drain the sender's two replies.

    Returns the `session_start_scheduled` broadcast the Wizard also receives
    (ADR-0003 §5: the start is announced, not applied instantly).
    """
    wizard.send_json(
        {
            "sequence": sequence,
            "client_timestamp_ms": 0,
            "event_type": "session_started",
            "payload": {"event_id": f"evt-start-{sequence}"},
        }
    )
    assert wizard.receive_json()["status"] == "accepted"
    scheduled = wizard.receive_json()
    assert scheduled["type"] == "session_start_scheduled"
    return scheduled


def _drain_start(sock) -> None:
    """Drain a non-Wizard socket's copies of the scheduled start."""
    assert sock.receive_json()["type"] == "session_start_scheduled"
    assert sock.receive_json()["event_type"] == "session_started"


def _assert_pong(sock, ping_id: str) -> None:
    """Round-trip a sentinel ping.

    Used to prove a socket has *nothing* queued: if a discarded command had
    been forwarded after all, it would be read here instead of the pong.
    """
    sock.send_json({"type": "ping", "ping_id": ping_id, "client_timestamp_ms": 0})
    reply = sock.receive_json()
    assert reply["type"] == "pong"
    assert reply["ping_id"] == ping_id


# --- Time manipulation (no real waiting) ------------------------------------


def _make_start_due(session_id: str) -> None:
    """Pull ADR-0003's scheduled start into the past.

    The alternative is sleeping through the real 2s start window in every test;
    this writes the same field `SessionSnapshot.resolve_scheduled_start` reads.
    """
    websocket_module.manager.snapshot_for(session_id).start_at_server_ms = 1


def _age_disconnect_mark(session_id: str, role: str) -> None:
    """Age a recorded disconnect past ADR-0002 §8's grace window.

    The rewind is deliberately measured against the *imported* (unpatched)
    `DISCONNECT_GRACE_MS`, so it stays far larger than any shortened window a
    test installs on the module: after this the elapsed-time check in
    `ConnectionManager.lost_critical_roles` cannot fail for timing reasons,
    whatever the host clock does.
    """
    marks = websocket_module.manager._disconnected_since[session_id]
    assert role in marks, f"the {role} socket close was never registered"
    marks[role] -= DISCONNECT_GRACE_MS + 1_000


def _mark_connection_stale(session_id: str, role: str) -> None:
    """Backdate a *live* connection's heartbeat past the takeover threshold.

    `ConnectionManager.connect` only lets a new socket displace an existing one
    for a single-connection role once the incumbent has missed heartbeats for
    longer than `HEARTBEAT_STALE_MS` (ADR-0001 §9) -- otherwise the newcomer is
    rejected with 4409. Backdating the tracked heartbeat is the only way to
    reach the takeover branch without idling for 30 real seconds.

    Note the window this opens is the same one `lost_critical_roles` reads: from
    here until the replacement connects, any inbound frame would see the role as
    lost and auto-pause the session (ADR-0002 §8). Callers must reconnect
    immediately and send nothing in between.
    """
    for info in websocket_module.manager._connections[session_id][role]:
        info.last_heartbeat_ms -= HEARTBEAT_STALE_MS + 1_000


# --- Read-back helpers ------------------------------------------------------


def _session_row(session_id: str) -> models.Session:
    with models.session_scope() as db:
        session = models.get_session(db, session_id)
    assert session is not None
    return session


def _stored_events(session_id: str) -> list[models.Event]:
    with models.session_scope() as db:
        return models.load_events(db, session_id)


def _audit_rows(session_id: str, action: str) -> list[models.AuditLog]:
    with models.session_scope() as db:
        return list(
            db.scalars(
                select(models.AuditLog)
                .where(models.AuditLog.session_id == session_id)
                .where(models.AuditLog.action == action)
                .order_by(models.AuditLog.id)
            )
        )


# --- M1 pass criterion 1: Wizard drives CASE001 playback --------------------


def test_full_multi_role_session_reaches_case001_playback(client):
    """Join -> start -> clip command -> student playback report, all four roles.

    The command carries a real CASE001 clip id, so this is the ADR-0004 M1
    criterion "the Wizard can drive CASE001 playback over WebSocket", end to
    end and with every role connected at once.
    """
    session_id = _new_session_id()
    headers = _join_roles(
        client, session_id, "wizard", "student", "teacher", "observer"
    )

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard, \
            _socket(client, session_id, "student", headers["student"]) as student, \
            _socket(client, session_id, "teacher", headers["teacher"]) as teacher, \
            _socket(client, session_id, "observer", headers["observer"]) as observer:
        scheduled = _start_session(wizard)
        assert scheduled["session_id"] == session_id
        for watcher in (student, teacher, observer):
            _drain_start(watcher)

        # The persisted row follows the snapshot into the start window.
        assert _session_row(session_id).status == "starting"
        _make_start_due(session_id)

        wizard.send_json(
            _clip_command(
                session_id,
                sequence=2,
                clip_id="C001_TIMELINE_LEAVE_02",
                command_id="cmd-timeline-1",
            )
        )
        assert wizard.receive_json() == {
            "type": "event_ack",
            "event_id": "cmd-timeline-1",
            "status": "accepted",
            "reason": None,
        }

        # Broadcast to every role except the sender (the Wizard already knows).
        for watcher in (student, teacher, observer):
            command = watcher.receive_json()
            assert command["event_type"] == "clip_command"
            assert command["source"] == "wizard"
            assert command["session_id"] == session_id
            assert command["payload"]["clip_id"] == "C001_TIMELINE_LEAVE_02"

        # The student plays it and reports back; the Wizard console needs that
        # confirmation to know the clip is on screen (spec §7).
        student.send_json(
            {
                "sequence": 3,
                "client_timestamp_ms": 3000,
                "event_type": "clip_started",
                "payload": {
                    "event_id": "evt-clip-started",
                    "clip_id": "C001_TIMELINE_LEAVE_02",
                },
            }
        )
        assert student.receive_json()["status"] == "accepted"
        for watcher in (wizard, teacher, observer):
            assert watcher.receive_json()["event_type"] == "clip_started"

        # ADR-0001 §9: review roles observe, they do not drive the session.
        observer.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "observer_note",
                "payload": {"event_id": "evt-observer-note", "note": "no"},
            }
        )
        ack = observer.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "read_only_role_cannot_emit_events"

        snapshot = websocket_module.manager.snapshot_for(session_id)
        assert snapshot.session_state == "active"
        assert snapshot.playback_state == "playing"
        assert snapshot.active_clip == "C001_TIMELINE_LEAVE_02"

    row = _session_row(session_id)
    assert row.status == "active"
    assert row.case_id == "CASE001"
    assert [e.event_type for e in _stored_events(session_id)] == [
        "session_started",
        "clip_command",
        "clip_started",
    ]


# --- M1 pass criterion 2: student clock sync --------------------------------


def test_student_clock_sync_completes_and_offset_is_applied_to_events(client):
    """ADR-0003: 9 probes -> lowest-5 median offset -> report -> stamped events.

    The probe exchange runs against the server's own monotonic clock, so the
    true offset for an in-process client is ~0. What is asserted is that the
    protocol completes over a real socket, that the computed offset is sane, and
    that the reported value is what later events are normalized against
    (ADR-0003 §7).
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student")

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard, \
            _socket(client, session_id, "student", headers["student"]) as student:
        samples: list[dict] = []
        for index in range(9):
            t0 = time.monotonic_ns() // 1_000_000
            student.send_json(
                {
                    "type": "clock_sync_probe",
                    "probe_id": f"probe-{index}",
                    "t0": t0,
                    "client_monotonic_ms": t0,
                }
            )
            response = student.receive_json()
            t3 = time.monotonic_ns() // 1_000_000
            assert response["type"] == "clock_sync_response"
            assert response["probe_id"] == f"probe-{index}"
            assert response["t0"] == t0
            assert t0 <= response["s1"] <= response["s2"] <= t3
            samples.append(
                {"t0": t0, "s1": response["s1"], "s2": response["s2"], "t3": t3}
            )

        assert len(samples) == 9
        offset_ms = int(compute_offset_median(samples, keep_lowest_rtt_count=5))
        rtt_ms = min((s["t3"] - s["t0"]) - (s["s2"] - s["s1"]) for s in samples)
        # Client and server share this process's monotonic clock, so a correct
        # implementation lands near zero; a sign error or a wall-clock mix-up
        # would be orders of magnitude off.
        assert abs(offset_ms) < 1_000
        assert rtt_ms >= 0

        student.send_json(
            {
                "type": "clock_offset_report",
                "offset_ms": 42,
                "sample_count": len(samples),
                "rtt_ms": rtt_ms,
            }
        )

        # ADR-0003 §5: the unified start is announced ~2s ahead on the same
        # monotonic scale the offset was computed against, which is what lets
        # the student schedule its own start.
        trigger_ms = time.monotonic_ns() // 1_000_000
        scheduled = _start_session(wizard)
        assert 1_900 <= scheduled["start_at_server_ms"] - trigger_ms <= 2_500
        _drain_start(student)

        # ADR-0003 §7: the reported offset is stamped onto the student's
        # subsequent events, with the version it was accepted under.
        student.send_json(
            {
                "sequence": 2,
                "client_timestamp_ms": 2000,
                "event_type": "recording_started",
                "payload": {"event_id": "evt-recording-started"},
            }
        )
        assert student.receive_json()["status"] == "accepted"
        forwarded = wizard.receive_json()
        assert forwarded["event_type"] == "recording_started"
        assert forwarded["clock_offset_ms"] == 42
        assert forwarded["payload"]["clock_offset_version"] == 2
        assert forwarded["payload"]["normalized_server_timestamp_ms"] == 2000 + 42

    stored = {e.event_type: e for e in _stored_events(session_id)}
    assert stored["recording_started"].clock_offset_ms == 42


# --- M1 pass criterion 3: no stale command ever reaches the student ---------


def test_expired_and_stale_commands_never_reach_the_student(client):
    """ADR-0001 §5 / ADR-0004 §1, from the student's point of view.

    A client that queued commands while offline is the realistic source of both
    failures, and the guarantee that matters end to end is the same for each:
    the student is never told to play them, and they leave no trace in the
    timeline.
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student")

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard, \
            _socket(client, session_id, "student", headers["student"]) as student:
        _start_session(wizard)
        _drain_start(student)
        _make_start_due(session_id)

        # Held past its deadline while the sender was offline.
        wizard.send_json(
            _clip_command(
                session_id,
                sequence=2,
                clip_id="C001_TIMELINE_LEAVE_02",
                command_id="cmd-expired",
                expires_at_ms=1,
            )
        )
        ack = wizard.receive_json()
        assert ack["event_id"] == "cmd-expired"
        assert ack["status"] == "rejected"
        assert ack["reason"] == "command_expired"

        # Composed against the pre-start snapshot, i.e. superseded state.
        wizard.send_json(
            _clip_command(
                session_id,
                sequence=3,
                clip_id="C001_TIMELINE_LEAVE_02",
                command_id="cmd-stale",
                snapshot_revision=0,
            )
        )
        ack = wizard.receive_json()
        assert ack["event_id"] == "cmd-stale"
        assert ack["status"] == "rejected"
        assert ack["reason"] == "stale_snapshot_revision"

        _assert_pong(student, "nothing-was-forwarded")

        snapshot = websocket_module.manager.snapshot_for(session_id)
        assert snapshot.playback_state == "idle"
        assert snapshot.active_clip is None

        # A correctly composed command still works: the rejections above are
        # the validity rule doing its job, not a wedged session.
        wizard.send_json(
            _clip_command(
                session_id,
                sequence=4,
                clip_id="C001_TIMELINE_LEAVE_02",
                command_id="cmd-fresh",
            )
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert student.receive_json()["payload"]["command_id"] == "cmd-fresh"

    # ADR-0004 §4: rejected commands are not events -- nothing durable is left
    # behind for the teacher timeline to replay.
    stored = _stored_events(session_id)
    assert [e.event_type for e in stored] == ["session_started", "clip_command"]
    assert [e.dedup_key for e in stored if e.event_type == "clip_command"] == [
        "cmd-fresh"
    ]


# --- M1 pass criterion 5: the four fallback categories ----------------------


def test_fallback_matrix_resolves_every_category_and_alerts_at_three(client):
    """ADR-0002 §2/§4/§5 over a live session, against the real CASE001 manifest.

    CASE001 ships exactly one approved fallback clip (CLARIFY). ADR-0002 §4
    forbids answering with a different category, so the other three resolve to
    their generic clips rather than borrowing the CLARIFY one -- that
    substitution is the behaviour under test, not a gap in the case.
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student")
    expected = {
        "CLARIFY": "C001_FALLBACK_CLARIFY_01",
        "ONE_AT_A_TIME": "C001_FALLBACK_ONEATATIME_01",
        "UNKNOWN_OR_UNSURE": "C001_FALLBACK_UNKNOWN_01",
        "DECLINE_OR_BOUNDARY": "C001_FALLBACK_DECLINE_01",
    }

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard, \
            _socket(client, session_id, "student", headers["student"]) as student:
        _start_session(wizard)
        _drain_start(student)
        _make_start_due(session_id)

        for index, (category, clip_id) in enumerate(expected.items(), start=2):
            wizard.send_json(
                _fallback_request(
                    session_id,
                    sequence=index,
                    category=category,
                    command_id=f"cmd-{category}",
                    reason="ambiguous_question",
                )
            )
            assert wizard.receive_json() == {
                "type": "event_ack",
                "event_id": f"cmd-{category}",
                "status": "accepted",
                "reason": None,
            }

            streak = index - 1
            # ADR-0002 §1: the Wizard asked for a category, so it learns the
            # resolved clip from the same broadcast the student plays.
            for sock in (wizard, student):
                used = sock.receive_json()
                assert used["event_type"] == "fallback_used"
                assert used["source"] == "wizard"
                assert used["payload"]["fallback_category"] == category
                assert used["payload"]["resolved_clip_id"] == clip_id
                assert used["payload"]["wizard_selected"] is True
                assert used["payload"]["reason"] == "ambiguous_question"
                assert used["payload"]["consecutive_fallbacks"] == streak

            if streak >= FALLBACK_THRESHOLD:
                # ADR-0002 §5: from the third consecutive fallback on, every
                # role is told the interview needs a human to step in.
                for sock in (wizard, student):
                    alert = sock.receive_json()
                    assert alert["type"] == "fallback_threshold_reached"
                    assert alert["payload"] == {"consecutive_fallbacks": streak}

        # A real case answer ends the streak (ADR-0002 §5).
        wizard.send_json(
            _clip_command(
                session_id,
                sequence=6,
                clip_id="C001_TIMELINE_LEAVE_02",
                command_id="cmd-real-answer",
            )
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert student.receive_json()["payload"]["clip_id"] == "C001_TIMELINE_LEAVE_02"
        assert (
            websocket_module.manager.snapshot_for(session_id).consecutive_fallbacks == 0
        )

        # The count travels in the snapshot, so a reconnecting Wizard restores
        # its (now cleared) warning state.
        wizard.send_json(
            _reconnect_hello(session_id, "wizard", last_snapshot_revision=0)
        )
        assert wizard.receive_json()["consecutive_fallbacks"] == 0

    fallback_events = [
        e for e in _stored_events(session_id) if e.event_type == "fallback_used"
    ]
    assert [e.payload["resolved_clip_id"] for e in fallback_events] == list(
        expected.values()
    )


# --- M1 pass criteria 3 + 4: disconnect, auto-pause, reconnect --------------


def test_critical_disconnect_auto_pauses_and_reconnect_restores_state(client):
    """ADR-0002 §8 then ADR-0001 §4: the student drops mid-interview and returns.

    The two criteria are tested together because that is the single flow they
    describe: the pause must be recorded with `network_failure`, and the
    returning client must be handed the *current* state rather than a replay of
    what it missed.
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student")

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard:
        # Opened without a `with` block: this socket is dropped mid-test to
        # simulate the student's network failing.
        student = _socket(client, session_id, "student", headers["student"])
        student.__enter__()
        _start_session(wizard)
        _drain_start(student)
        _make_start_due(session_id)

        wizard.send_json(
            _clip_command(
                session_id,
                sequence=2,
                clip_id="C001_TIMELINE_LEAVE_02",
                command_id="cmd-before-drop",
            )
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert student.receive_json()["payload"]["command_id"] == "cmd-before-drop"
        revision_before_drop = _live_revision(session_id)

        student.__exit__(None, None, None)
        _age_disconnect_mark(session_id, "student")

        # The Wizard's next frame is evaluated *after* the loss is noticed, so
        # the pause arrives ahead of the reply to it (ADR-0002 §8).
        wizard.send_json(
            {"type": "ping", "ping_id": "after-drop", "client_timestamp_ms": 0}
        )
        paused = wizard.receive_json()
        assert paused["event_type"] == "session_paused"
        assert paused["source"] == "server"
        assert paused["payload"]["reason"] == "network_failure"
        assert paused["payload"]["lost_roles"] == ["student"]
        assert paused["payload"]["auto_paused"] is True
        assert wizard.receive_json()["ping_id"] == "after-drop"

        row = _session_row(session_id)
        assert row.status == "paused"
        assert row.pause_reason == "network_failure"
        # ADR-0004 §3: attributed to the server, not to a human who did not act.
        audits = _audit_rows(session_id, models.AUDIT_SESSION_PAUSED)
        assert [a.actor_role for a in audits] == ["server"]
        assert audits[0].details["reason"] == "network_failure"

        # The student comes back on a fresh socket with the same cookie.
        with _socket(client, session_id, "student", headers["student"]) as student:
            student.send_json(
                _reconnect_hello(
                    session_id, "student", last_snapshot_revision=revision_before_drop
                )
            )
            snapshot = student.receive_json()
            assert snapshot["type"] == "session_snapshot"
            assert snapshot["session_state"] == "paused"
            assert snapshot["snapshot_revision"] > revision_before_drop
            # Snapshot-first, not replay-first: the command issued before the
            # drop has expired by now and must not be pushed again.
            _assert_pong(student, "no-replay-after-reconnect")

            # The Wizard resumes now the student is back (ADR-0004 §2).
            wizard.send_json(
                {
                    "sequence": 3,
                    "client_timestamp_ms": 3000,
                    "event_type": "session_resumed",
                    "payload": {"event_id": "evt-resume"},
                }
            )
            assert wizard.receive_json()["status"] == "accepted"
            assert student.receive_json()["event_type"] == "session_resumed"
            assert (
                websocket_module.manager.snapshot_for(session_id).session_state
                == "active"
            )

    assert _session_row(session_id).status == "active"
    paused_events = [
        e for e in _stored_events(session_id) if e.event_type == "session_paused"
    ]
    assert len(paused_events) == 1
    assert paused_events[0].source == "server"
    assert paused_events[0].payload["lost_roles"] == ["student"]


def test_wizard_disconnect_pauses_without_any_further_inbound_traffic(
    client, monkeypatch
):
    """The other critical role, on the path where no message triggers the check.

    Nothing arrives after the Wizard's socket closes, so the pause has to come
    from the ADR-0002 §8 grace-window timer rather than from the check every
    inbound frame runs. The window is shortened rather than slept through.

    Two separate things have to hold for that timer to be exercised rather than
    raced, and both are arranged below:

    * The shortened window must stay well above the event loop's clock
      resolution. `_delayed_disconnect_pause_check` waits with
      `asyncio.sleep(DISCONNECT_GRACE_MS / 1000)`, and asyncio fires a timer as
      soon as it is within one `_clock_resolution` of being due. That
      resolution follows `time.monotonic()`, which ticks about every 15.6ms on
      Windows -- so a 10ms window would elapse *instantly*, the check would run
      before the disconnect had aged at all, find nothing lost, and return
      without ever rescheduling. The receive below would then block forever.
    * The elapsed-time comparison itself must not depend on how long that sleep
      really lasted, so the disconnect mark is aged once the socket is down.
    """
    session_id = _new_session_id()
    monkeypatch.setattr(websocket_module, "DISCONNECT_GRACE_MS", 300)
    headers = _join_roles(client, session_id, "wizard", "student", "teacher")

    with _socket(client, session_id, "student", headers["student"]) as student, \
            _socket(client, session_id, "teacher", headers["teacher"]) as teacher:
        wizard = _socket(client, session_id, "wizard", headers["wizard"])
        wizard.__enter__()
        _start_session(wizard)
        _drain_start(student)
        _drain_start(teacher)
        _make_start_due(session_id)

        wizard.__exit__(None, None, None)
        # The timer is still inside its (now 300ms) window at this point, so
        # this lands before the check it guards -- the same ordering the
        # student-side disconnect test relies on.
        _age_disconnect_mark(session_id, "wizard")

        # Blocking on the broadcast is also what keeps this test off the
        # database while the handler thread is writing to it: StaticPool shares
        # one SQLite connection, so polling the row from here would interleave
        # this thread's transactions with the pause's own.
        for sock in (student, teacher):
            paused = sock.receive_json()
            assert paused["event_type"] == "session_paused"
            assert paused["payload"]["reason"] == "network_failure"
            assert paused["payload"]["lost_roles"] == ["wizard"]
            assert paused["payload"]["auto_paused"] is True

    row = _session_row(session_id)
    assert row.status == "paused"
    assert row.pause_reason == "network_failure"
    assert [
        a.actor_role for a in _audit_rows(session_id, models.AUDIT_SESSION_PAUSED)
    ] == ["server"]


def test_student_takeover_is_audited_without_pausing_the_live_interview(client):
    """ADR-0001 §9 against ADR-0002 §8: a browser reload is not a lost role.

    The student's tab is refreshed mid-interview, so the old socket is still
    registered (half-open, missing heartbeats) when the new one arrives. Two
    rules meet here, and only end to end:

    * The newcomer takes over rather than being rejected, the other roles are
      told, and the takeover is audited (ADR-0004 §3).
    * The session stays `active`. The §8 auto-pause is for a role that is
      *gone*, and `manager.connect` clears the disconnect mark as part of
      accepting the replacement -- so a reconnect must not be recorded as a
      technical failure the teacher later sees in the timeline.

    `test_websocket.py` covers the takeover itself, but on a session still in
    `created`, where the §8 pause cannot fire either way.
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student", "teacher")

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard, \
            _socket(client, session_id, "teacher", headers["teacher"]) as teacher:
        # Opened without a `with` block: the server closes this socket itself
        # during the takeover, so its teardown is handled explicitly below.
        stale_student = _socket(client, session_id, "student", headers["student"])
        stale_student.__enter__()
        try:
            _start_session(wizard)
            _drain_start(stale_student)
            _drain_start(teacher)
            _make_start_due(session_id)
            revision_before_reload = _live_revision(session_id)

            _mark_connection_stale(session_id, "student")

            with _socket(client, session_id, "student", headers["student"]) as student:
                for sock in (wizard, teacher):
                    notice = sock.receive_json()
                    assert notice["type"] == "role_connection_replaced"
                    assert notice["role"] == "student"

                # The reloaded tab has lost its local state, so it asks for the
                # session rather than assuming it (ADR-0001 §4).
                student.send_json(
                    _reconnect_hello(session_id, "student", last_snapshot_revision=0)
                )
                snapshot = student.receive_json()
                assert snapshot["type"] == "session_snapshot"
                assert snapshot["session_state"] == "active"
                assert snapshot["snapshot_revision"] == revision_before_reload

                # And the interview carries on over the new socket.
                wizard.send_json(
                    _clip_command(
                        session_id,
                        sequence=2,
                        clip_id="C001_TIMELINE_LEAVE_02",
                        command_id="cmd-after-reload",
                    )
                )
                assert wizard.receive_json()["status"] == "accepted"
                for sock in (student, teacher):
                    assert sock.receive_json()["payload"]["command_id"] == (
                        "cmd-after-reload"
                    )
        finally:
            try:
                stale_student.__exit__(None, None, None)
            except Exception:
                pass  # already closed server-side by the takeover

    row = _session_row(session_id)
    assert row.status == "active"
    assert row.pause_reason is None

    # ADR-0004 §3: the takeover is on the record, without anything token-shaped.
    takeovers = _audit_rows(session_id, models.AUDIT_CONNECTION_TAKEOVER)
    assert [a.actor_role for a in takeovers] == ["student"]
    assert takeovers[0].token_hash is None
    assert takeovers[0].details["replaced_role"] == "student"

    # Nothing in the durable timeline claims the session ever dropped.
    assert not [
        e for e in _stored_events(session_id) if e.event_type == "session_paused"
    ]
    assert _audit_rows(session_id, models.AUDIT_SESSION_PAUSED) == []


def test_events_and_recording_chunks_are_backfilled_after_a_brief_reconnect(
    client, chunk_root
):
    """ADR-0004 M1: "事件與錄影 chunk 可以補送" -- nothing is lost to a short drop.

    The student's connection blips while recording. The browser keeps capturing
    and queueing, so on return it has three things to hand over: the event whose
    ACK was lost in the drop, the events it produced while offline, and the chunk
    it buffered. The guarantee is that the retry does *not* double up (ADR-0001
    §7) while the genuinely new work does land, so the teacher timeline and the
    recording are both complete afterwards.

    The reconnect is deliberately inside `DISCONNECT_GRACE_MS`, which is the
    whole point of ADR-0002 §8's window: a blip is smoothed over rather than
    escalated to a technical pause.

    Chunk ingest itself (`recording.py`) is still a stub -- see the note in the
    implementation notes. What is asserted here is the M1 half: a chunk sent
    late, out of the live stream, is stored under its own sequence and does not
    disturb the ones already on disk.
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student")

    def _upload_chunk(sequence: int) -> None:
        resp = client.post(
            f"/recording/{session_id}/chunk",
            data={"sequence": sequence},
            files={"chunk": (f"{sequence}.webm", b"webm-" + str(sequence).encode(), "video/webm")},
            headers=headers["student"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["sequence"] == sequence

    def _chunk_event(sequence: int, chunk_index: int) -> dict:
        return {
            "sequence": sequence,
            "client_timestamp_ms": sequence * 1000,
            "event_type": "chunk_uploaded",
            "payload": {
                "event_id": f"evt-chunk-{chunk_index}",
                "chunk_index": chunk_index,
            },
        }

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard:
        # Opened without a `with` block: this socket is dropped mid-test.
        student = _socket(client, session_id, "student", headers["student"])
        student.__enter__()
        _start_session(wizard)
        _drain_start(student)
        _make_start_due(session_id)

        student.send_json(
            {
                "sequence": 2,
                "client_timestamp_ms": 2000,
                "event_type": "recording_started",
                "payload": {"event_id": "evt-recording-started"},
            }
        )
        assert student.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["event_type"] == "recording_started"

        for chunk_index in (1, 2):
            _upload_chunk(chunk_index)
            student.send_json(_chunk_event(2 + chunk_index, chunk_index))
            assert student.receive_json()["status"] == "accepted"
            assert wizard.receive_json()["event_type"] == "chunk_uploaded"

        revision_before_drop = _live_revision(session_id)
        student.__exit__(None, None, None)

        # Back well inside the grace window -- the disconnect mark is left
        # un-aged on purpose, so ADR-0002 §8 must not have escalated it.
        with _socket(client, session_id, "student", headers["student"]) as student:
            student.send_json(
                _reconnect_hello(
                    session_id, "student", last_snapshot_revision=revision_before_drop
                )
            )
            snapshot = student.receive_json()
            assert snapshot["type"] == "session_snapshot"
            assert snapshot["session_state"] == "active"
            assert snapshot["snapshot_revision"] == revision_before_drop

            # 1. The in-flight event whose ACK never arrived, re-sent blind.
            student.send_json(_chunk_event(4, 2))
            assert student.receive_json()["status"] == "duplicate"

            # 2. What the browser captured while it was offline.
            _upload_chunk(3)
            student.send_json(_chunk_event(5, 3))
            assert student.receive_json()["status"] == "accepted"
            assert wizard.receive_json()["payload"]["chunk_index"] == 3

            student.send_json(
                {
                    "sequence": 6,
                    "client_timestamp_ms": 6000,
                    "event_type": "recording_stopped",
                    "payload": {"event_id": "evt-recording-stopped"},
                }
            )
            assert student.receive_json()["status"] == "accepted"
            assert wizard.receive_json()["event_type"] == "recording_stopped"

    # The session was never paused: a blip inside the window is not a technical
    # failure (ADR-0002 §8).
    row = _session_row(session_id)
    assert row.status == "active"
    assert row.pause_reason is None

    # The timeline the teacher replays holds each event exactly once, in order,
    # with the offline stretch filled in (ADR-0004 §4).
    events = client.get(f"/sessions/{session_id}/events").json()["events"]
    assert [e["event_type"] for e in events] == [
        "session_started",
        "recording_started",
        "chunk_uploaded",
        "chunk_uploaded",
        "chunk_uploaded",
        "recording_stopped",
    ]
    assert [
        e["payload"]["chunk_index"] for e in events if e["event_type"] == "chunk_uploaded"
    ] == [1, 2, 3]
    assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)

    # And so does the recording: the chunk buffered during the drop is stored
    # alongside the ones that preceded it, not in place of them.
    stored = sorted(p.name for p in (chunk_root / session_id / "chunks").glob("*.webm"))
    assert stored == ["000001.webm", "000002.webm", "000003.webm"]


# --- M1 pass criterion 6: replay + audit trail ------------------------------


def test_session_replay_and_audit_trail_back_the_teacher_timeline(client):
    """ADR-0004 §3/§4: what the teacher app reads back after the session.

    Everything asserted after the sockets close is read over REST or out of the
    audit table -- the same surface the review app uses, with the realtime
    state gone.
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student", "teacher")

    with _socket(client, session_id, "wizard", headers["wizard"]) as wizard, \
            _socket(client, session_id, "student", headers["student"]) as student, \
            _socket(client, session_id, "teacher", headers["teacher"]) as teacher:
        _start_session(wizard)
        for watcher in (student, teacher):
            _drain_start(watcher)
        _make_start_due(session_id)

        wizard.send_json(
            _clip_command(
                session_id,
                sequence=2,
                clip_id="C001_TIMELINE_LEAVE_02",
                command_id="cmd-replay",
            )
        )
        assert wizard.receive_json()["status"] == "accepted"
        for watcher in (student, teacher):
            assert watcher.receive_json()["event_type"] == "clip_command"

        # spec §6/§8: the Wizard's `M` hotkey, routed to review roles only.
        wizard.send_json(
            {
                "sequence": 3,
                "client_timestamp_ms": 3000,
                "event_type": "uncovered_question",
                "payload": {
                    "event_id": "evt-uncovered",
                    "question_text": "when did you get home?",
                    "note": "no approved clip covers this",
                },
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        marked = teacher.receive_json()
        assert marked["event_type"] == "uncovered_question"
        assert marked["payload"]["question_text"] == "when did you get home?"
        # The student is mid-interview and must not see the annotation.
        _assert_pong(student, "student-sees-no-annotation")

        # A retry after a missed ACK must not double up in the replay
        # (ADR-0001 §7).
        wizard.send_json(
            {
                "sequence": 3,
                "client_timestamp_ms": 3000,
                "event_type": "uncovered_question",
                "payload": {"event_id": "evt-uncovered", "question_text": "retry"},
            }
        )
        assert wizard.receive_json()["status"] == "duplicate"

        wizard.send_json(
            {
                "sequence": 4,
                "client_timestamp_ms": 4000,
                "event_type": "session_ended",
                "payload": {"event_id": "evt-ended"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        for watcher in (student, teacher):
            assert watcher.receive_json()["event_type"] == "session_ended"

    # The unified timeline, in sequence order, over REST.
    replay = client.get(f"/sessions/{session_id}/events")
    assert replay.status_code == 200
    events = replay.json()["events"]
    assert [e["event_type"] for e in events] == [
        "session_started",
        "clip_command",
        "uncovered_question",
        "session_ended",
    ]
    assert all(e["session_id"] == session_id for e in events)
    assert all(e["server_timestamp_ms"] is not None for e in events)
    assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)

    assert client.get(f"/sessions/{session_id}").json()["status"] == "ended"

    # ADR-0004 §3: one audit row per issued token, hashed, never raw.
    issued = _audit_rows(session_id, models.AUDIT_TOKEN_ISSUED)
    assert sorted(a.actor_role for a in issued) == ["student", "teacher", "wizard"]
    for row in issued:
        assert row.token_hash is not None
        assert len(row.token_hash) == 64
        assert "." not in row.token_hash


def _upload_chunk(client, session_id: str, sequence: int, headers: dict[str, str]) -> None:
    resp = client.post(
        f"/recording/{session_id}/chunk",
        data={"sequence": sequence},
        files={"chunk": (f"{sequence}.webm", f"webm-{sequence}".encode(), "video/webm")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sequence"] == sequence


def test_wizard_start_leaves_pending_uploads_open_without_starting_grace(
    client, chunk_root
):
    """開始場次 does not create a session, revoke tokens, or open upload grace.

    A chunk queued before start still uploads while the session is `starting`.
    Grace begins only after the existing end route sets `ended_at`, and a
    chunk still pending then is accepted on that same path.
    """
    session_id = _new_session_id()
    headers = _join_roles(client, session_id, "wizard", "student")
    wizard_token = headers["wizard"]["cookie"].split("=", 1)[1]
    student_token = headers["student"]["cookie"].split("=", 1)[1]

    for token in (wizard_token, student_token):
        created = client.post(
            "/sessions",
            json={"session_id": f"{session_id}-copy", "case_id": "CASE001"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 401
        assert "join_codes" not in created.text
    with models.session_scope() as db:
        assert models.get_session(db, f"{session_id}-copy") is None

    _upload_chunk(client, session_id, 0, headers["student"])

    with (
        _socket(client, session_id, "wizard", headers["wizard"]) as wizard,
        _socket(client, session_id, "student", headers["student"]) as student,
    ):
        student.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "evt-student-start"},
            }
        )
        refused = student.receive_json()
        assert refused["status"] == "rejected"
        assert refused["reason"] == "session_start_requires_wizard"
        _assert_pong(wizard, "wizard-not-started")

        _start_session(wizard)
        _drain_start(student)

        row = _session_row(session_id)
        assert row.status == "starting"
        assert row.ended_at is None
        assert auth.session_upload_grace_open(row) is False
        _upload_chunk(client, session_id, 1, headers["student"])

    ended = client.post(f"/sessions/{session_id}/end", headers=headers["wizard"])
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "ended"
    assert ended.json()["ended_at"]
    row = _session_row(session_id)
    assert auth.session_upload_grace_open(row) is True
    _upload_chunk(client, session_id, 2, headers["student"])

    stored = {sequence for sequence, _path in recording.stored_chunks(session_id)}
    assert stored == {0, 1, 2}
