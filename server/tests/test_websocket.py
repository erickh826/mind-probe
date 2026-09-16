"""WebSocket connection manager tests (ADR-0001; ADR-0002; ADR-0003; ADR-0004).

Also covers the realtime persistence/audit wiring: the session-existence
guard on upgrade, event write-through to SQLite, lifecycle state mirroring,
and the `connection_takeover` audit row (ADR-0004 §1-§4) -- plus the ADR-0002
fallback resolution, consecutive-fallback threshold, and disconnect-triggered
technical pause.

Every test runs against its own in-memory SQLite database, swapped in by the
autouse `db_factory` fixture below, so nothing here touches the real
`sessions/mind-probe.sqlite`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("MINDPROBE_WS_TOKEN_SECRET", "test-websocket-secret")

from app import cases, models, websocket as websocket_module
from app.events import ClipManifestEntry
from app.main import app
from app.models import Base
from app.websocket import (
    DISCONNECT_GRACE_MS,
    FALLBACK_THRESHOLD,
    HEARTBEAT_STALE_MS,
    WS_SESSION_COOKIE,
    compute_offset_median,
    create_session_token,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def db_factory(monkeypatch):
    """Point `models.SessionLocal` at a private in-memory database.

    `websocket.py` reaches the database through `models.session_scope()`, which
    resolves `SessionLocal` at call time -- so patching it here exercises the
    production code path (rather than the `get_db` dependency override, which
    is covered by `test_persistence.py`) while keeping every test isolated.

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
    monkeypatch.setattr(models, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _unpersisted_session_id() -> str:
    """A unique session_id with no row behind it.

    Ids are unique per test so `manager`'s process-wide in-memory state cannot
    leak between them.
    """
    return f"S{uuid4().hex}"


def _fresh_session_id(case_id: str = "CASE001") -> str:
    """A unique session_id, persisted so the upgrade guard accepts it.

    Sockets are refused for a `session_id` with no row -- an event that could
    not be persisted must not be accepted at all (ADR-0004 §4) -- so every test
    that connects needs the session to exist first. Goes through
    `models.session_scope()`, which the autouse fixture has pointed at this
    test's own database. `case_id` is what the socket upgrade memoises for
    ADR-0002 fallback resolution.
    """
    session_id = _unpersisted_session_id()
    with models.session_scope() as db:
        models.create_session(db, session_id, case_id)
    return session_id


def _auth_headers(session_id: str, role: str) -> dict[str, str]:
    token = create_session_token(session_id, role)
    return {"cookie": f"{WS_SESSION_COOKIE}={token}"}


def _connect(session_id: str, role: str, *, token_role: str | None = None):
    return client.websocket_connect(
        f"/ws/{session_id}/{role}",
        headers=_auth_headers(session_id, token_role or role),
    )


def test_heartbeat_ping_gets_pong_with_server_timestamp():
    session_id = _fresh_session_id()
    with _connect(session_id, "student") as sock:
        sock.send_json({"type": "ping", "ping_id": "p1", "client_timestamp_ms": 100})
        reply = sock.receive_json()
        assert reply["type"] == "pong"
        assert reply["ping_id"] == "p1"
        assert isinstance(reply["server_timestamp_ms"], int)


def test_reconnect_hello_returns_session_snapshot_first():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as sock:
        sock.send_json(
            {
                "type": "reconnect_hello",
                "session_id": session_id,
                "role": "wizard",
                "last_snapshot_revision": 0,
                "last_acked_sequence": 0,
                "pending_event_ids": [],
            }
        )
        snapshot = sock.receive_json()
        assert snapshot["type"] == "session_snapshot"
        assert snapshot["snapshot_revision"] == 0
        assert snapshot["session_state"] == "created"
        assert snapshot["playback_state"] == "idle"
        assert "server_timestamp_ms" in snapshot


def test_duplicate_event_id_is_acked_as_duplicate_and_not_rebroadcast():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        event = {
            "sequence": 1,
            "client_timestamp_ms": 0,
            "event_type": "observer_note",
            "payload": {"event_id": "evt-1", "note": "hello"},
        }
        wizard.send_json(event)
        ack1 = wizard.receive_json()
        assert ack1 == {
            "type": "event_ack",
            "event_id": "evt-1",
            "status": "accepted",
            "reason": None,
        }
        forwarded = student.receive_json()
        assert forwarded["payload"]["event_id"] == "evt-1"

        # Resend the same event_id (simulating a retry after a missed ACK).
        wizard.send_json(event)
        ack2 = wizard.receive_json()
        assert ack2 == {
            "type": "event_ack",
            "event_id": "evt-1",
            "status": "duplicate",
            "reason": None,
        }
        # The student side must not receive a second broadcast.
        student.send_json({"type": "ping", "ping_id": "sentinel", "client_timestamp_ms": 0})
        sentinel_reply = student.receive_json()
        assert sentinel_reply["ping_id"] == "sentinel"


def test_expired_short_lived_command_is_rejected_and_not_forwarded():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        expired_command = {
            "sequence": 1,
            "client_timestamp_ms": 0,
            "event_type": "clip_command",
            "payload": {
                "command_id": "cmd-1",
                "expires_at_ms": 1,  # already in the past
                "snapshot_revision": 0,
                "clip_id": "C001",
            },
        }
        wizard.send_json(expired_command)
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "command_expired"

        # The expired command must not reach the student.
        student.send_json({"type": "ping", "ping_id": "sentinel", "client_timestamp_ms": 0})
        sentinel_reply = student.receive_json()
        assert sentinel_reply["ping_id"] == "sentinel"


def test_stale_snapshot_revision_command_is_rejected():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        # Bump snapshot_revision to 1 via an accepted session_started event.
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "evt-start"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        # session_started also triggers the ADR-0003 §5 scheduled-start
        # broadcast, which the wizard (a session participant) also receives.
        assert wizard.receive_json()["type"] == "session_start_scheduled"

        stale_command = {
            "sequence": 2,
            "client_timestamp_ms": 0,
            "event_type": "clip_command",
            "payload": {
                "command_id": "cmd-2",
                "expires_at_ms": _far_future_ms(),
                "snapshot_revision": 0,  # stale: session is already at revision 1
                "clip_id": "C001",
            },
        }
        wizard.send_json(stale_command)
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "stale_snapshot_revision"


def test_session_paused_after_start_accepts_valid_pause_reason_and_updates_snapshot():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "start-before-pause"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["type"] == "session_start_scheduled"

        wizard.send_json(
            {
                "sequence": 2,
                "client_timestamp_ms": 0,
                "event_type": "session_paused",
                "payload": {
                    "event_id": "pause-1",
                    "command_id": "pause-1",
                    "expires_at_ms": _far_future_ms(),
                    "snapshot_revision": 1,
                    "reason": "network_failure",
                },
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "accepted"

        wizard.send_json(
            {
                "type": "reconnect_hello",
                "session_id": session_id,
                "role": "wizard",
                "last_snapshot_revision": 0,
                "last_acked_sequence": 0,
                "pending_event_ids": [],
            }
        )
        snapshot = wizard.receive_json()
        assert snapshot["session_state"] == "paused"
        assert snapshot["snapshot_revision"] == 2


def test_session_paused_rejects_invalid_pause_reason():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_paused",
                "payload": {
                    "event_id": "pause-2",
                    "command_id": "pause-2",
                    "expires_at_ms": _far_future_ms(),
                    "snapshot_revision": 0,
                    "reason": "bad_reason",
                },
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "invalid_pause_reason"


def test_session_paused_rejects_missing_command_fields():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_paused",
                "payload": {"event_id": "pause-3", "reason": "network_failure"},
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "missing_command_fields"


def test_envelope_source_spoofing_is_rejected():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "session_id": session_id,
                "source": "student",
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "observer_note",
                "payload": {"event_id": "spoof-1"},
            }
        )
        ack = wizard.receive_json()
        assert ack == {
            "type": "event_ack",
            "event_id": "spoof-1",
            "status": "rejected",
            "reason": "source_role_mismatch",
        }


def test_malformed_short_lived_command_is_rejected_without_crashing():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "clip_command",
                "payload": {
                    "command_id": "cmd-bad",
                    "expires_at_ms": "not-an-int",
                    "snapshot_revision": 0,
                    "clip_id": "C001",
                },
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "invalid_command_fields"


def test_non_object_json_message_is_rejected_without_crashing():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(["not", "an", "object"])
        ack = wizard.receive_json()
        assert ack == {
            "type": "event_ack",
            "event_id": None,
            "status": "rejected",
            "reason": "invalid_message",
        }

        wizard.send_json({"type": "ping", "ping_id": "after-array", "client_timestamp_ms": 0})
        assert wizard.receive_json()["ping_id"] == "after-array"


def test_invalid_json_text_is_rejected_without_crashing():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_text("{not-json")
        ack = wizard.receive_json()
        assert ack == {
            "type": "event_ack",
            "event_id": None,
            "status": "rejected",
            "reason": "invalid_json",
        }

        wizard.send_json({"type": "ping", "ping_id": "after-invalid-json", "client_timestamp_ms": 0})
        assert wizard.receive_json()["ping_id"] == "after-invalid-json"


def test_unauthenticated_websocket_is_rejected():
    session_id = _fresh_session_id()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/{session_id}/student"):
            pass
    assert exc_info.value.code == 4401


def test_role_mismatched_token_is_rejected():
    session_id = _fresh_session_id()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _connect(session_id, "wizard", token_role="student"):
            pass
    assert exc_info.value.code == 4401


def test_second_student_connection_is_rejected_while_first_is_live():
    session_id = _fresh_session_id()
    with _connect(session_id, "student"):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with _connect(session_id, "student"):
                pass
    assert exc_info.value.code == 4409


def test_invalid_role_is_rejected_at_handshake():
    session_id = _fresh_session_id()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/{session_id}/not_a_role"):
            pass
    assert exc_info.value.code == 4400


def _far_future_ms() -> int:
    return time.monotonic_ns() // 1_000_000 + 60_000


# --- ADR-0003 time-sync tests ----------------------------------------------


def _probe_sample(rtt_ms: int, offset_ms: int) -> dict:
    """Build a {t0, s1, s2, t3} sample yielding the given RTT and offset.

    Derived by fixing t0=0, s2=s1 (instant server processing), then solving
    RTT=(t3-t0)-(s2-s1) and offset=((s1-t0)+(s2-t3))/2 for s1/t3.
    """
    t0 = 0
    t3 = rtt_ms
    s1 = offset_ms + rtt_ms // 2
    s2 = s1
    return {"t0": t0, "s1": s1, "s2": s2, "t3": t3}


def test_clock_sync_probe_returns_response_with_matching_timestamps():
    session_id = _fresh_session_id()
    with _connect(session_id, "student") as sock:
        sock.send_json(
            {
                "type": "clock_sync_probe",
                "probe_id": "p1",
                "t0": 1000,
                "client_monotonic_ms": 1000,
            }
        )
        reply = sock.receive_json()
        assert reply["type"] == "clock_sync_response"
        assert reply["probe_id"] == "p1"
        assert reply["t0"] == 1000
        assert isinstance(reply["s1"], int)
        assert isinstance(reply["s2"], int)
        assert reply["s1"] <= reply["s2"]


def test_compute_offset_median_initial_sync_keeps_lowest_5_of_9():
    # rtt -> offset pairs; lowest-5-RTT subset is (10,12,14,20,24), offsets
    # (30,32,34,36,38), median 34. The remaining 4 have deliberately
    # different offsets so an unfiltered average/median would differ.
    samples = [
        _probe_sample(rtt_ms=50, offset_ms=100),
        _probe_sample(rtt_ms=10, offset_ms=30),
        _probe_sample(rtt_ms=80, offset_ms=120),
        _probe_sample(rtt_ms=14, offset_ms=34),
        _probe_sample(rtt_ms=20, offset_ms=36),
        _probe_sample(rtt_ms=90, offset_ms=130),
        _probe_sample(rtt_ms=24, offset_ms=38),
        _probe_sample(rtt_ms=12, offset_ms=32),
        _probe_sample(rtt_ms=70, offset_ms=110),
    ]
    assert compute_offset_median(samples, keep_lowest_rtt_count=5) == 34


def test_compute_offset_median_continuous_correction_keeps_lowest_3_of_5():
    # lowest-3-RTT subset is (6,8,40), offsets (60,62,200), median 62.
    samples = [
        _probe_sample(rtt_ms=40, offset_ms=200),
        _probe_sample(rtt_ms=6, offset_ms=60),
        _probe_sample(rtt_ms=44, offset_ms=210),
        _probe_sample(rtt_ms=8, offset_ms=62),
        _probe_sample(rtt_ms=50, offset_ms=220),
    ]
    assert compute_offset_median(samples, keep_lowest_rtt_count=3) == 62


def test_clock_offset_report_increments_version_and_applies_to_subsequent_events():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        wizard.send_json(
            {"type": "clock_offset_report", "offset_ms": 42, "sample_count": 5, "rtt_ms": 20}
        )
        # No reply is expected for clock_offset_report; the next event's
        # payload/clock_offset_ms is the observable effect.
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 1000,
                "event_type": "observer_note",
                "payload": {"event_id": "evt-1", "note": "hello"},
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "accepted"

        forwarded = student.receive_json()
        assert forwarded["clock_offset_ms"] == 42
        assert forwarded["payload"]["clock_offset_version"] == 2
        assert forwarded["payload"]["normalized_server_timestamp_ms"] == 1000 + 42


def test_session_started_broadcasts_scheduled_start_two_seconds_ahead():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        trigger_monotonic_ms = int(time.monotonic() * 1000)
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "evt-start"},
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "accepted"

        scheduled_for_wizard = wizard.receive_json()
        assert scheduled_for_wizard["type"] == "session_start_scheduled"
        assert scheduled_for_wizard["session_id"] == session_id

        scheduled_for_student = student.receive_json()
        assert scheduled_for_student["type"] == "session_start_scheduled"

        delta_ms = scheduled_for_wizard["start_at_server_ms"] - trigger_monotonic_ms
        assert 1900 <= delta_ms <= 2500  # ~2000ms, with slack for test overhead

        forwarded_envelope = student.receive_json()
        assert forwarded_envelope["event_type"] == "session_started"


def test_reconnect_snapshot_during_scheduled_start_window_is_not_active():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "evt-start-reconnect"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        scheduled = wizard.receive_json()
        assert scheduled["type"] == "session_start_scheduled"

        wizard.send_json(
            {
                "type": "reconnect_hello",
                "session_id": session_id,
                "role": "wizard",
                "last_snapshot_revision": 0,
                "last_acked_sequence": 0,
                "pending_event_ids": [],
            }
        )
        snapshot = wizard.receive_json()
        assert snapshot["type"] == "session_snapshot"
        assert snapshot["snapshot_revision"] == 1
        assert snapshot["session_state"] == "starting"
        assert snapshot["start_at_server_ms"] == scheduled["start_at_server_ms"]


# --- Realtime persistence + audit wiring (ADR-0004 §1-§4) -------------------


def _session_row(session_id: str) -> models.Session:
    """Read the persisted session row back."""
    with models.session_scope() as db:
        session = models.get_session(db, session_id)
    assert session is not None
    return session


def _stored_events(session_id: str) -> list[models.Event]:
    with models.session_scope() as db:
        return models.load_events(db, session_id)


def _audit_rows(session_id: str, action: str) -> list[models.AuditLog]:
    """Audit rows for one action.

    Filtered by action rather than counted wholesale: `models.pause_session`
    writes its own `session_paused` row, so a total count would conflate the
    two writers.
    """
    with models.session_scope() as db:
        return list(
            db.scalars(
                select(models.AuditLog)
                .where(models.AuditLog.session_id == session_id)
                .where(models.AuditLog.action == action)
                .order_by(models.AuditLog.id)
            )
        )


def _mark_connection_stale(session_id: str, role: str) -> None:
    """Backdate a tracked connection's heartbeat past the takeover threshold.

    `manager.connect` only replaces a single-connection role once it has missed
    heartbeats for longer than HEARTBEAT_STALE_MS (ADR-0001 §9); reaching into
    the tracked ConnectionInfo is the only way to reach that branch without
    waiting 30 real seconds.
    """
    for info in websocket_module.manager._connections[session_id][role]:
        info.last_heartbeat_ms -= HEARTBEAT_STALE_MS + 1_000


def test_connect_to_unknown_session_is_rejected():
    # The token is validly signed for this session id -- only the row is
    # missing, so this exercises the persistence guard and not auth.
    session_id = _unpersisted_session_id()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _connect(session_id, "student"):
            pass
    assert exc_info.value.code == 4404


def test_accepted_event_is_persisted_to_sqlite():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 7,
                "client_timestamp_ms": 1234,
                "event_type": "observer_note",
                "payload": {"event_id": "evt-persist", "note": "hello"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"

    stored = _stored_events(session_id)
    assert len(stored) == 1
    event = stored[0]
    assert event.dedup_key == "evt-persist"
    assert event.sequence == 7
    assert event.source == "wizard"
    assert event.event_type == "observer_note"
    assert event.client_timestamp_ms == 1234
    assert event.server_timestamp_ms is not None
    assert event.payload["note"] == "hello"
    # ADR-0003 §7: the offset stamping is persisted as applied at accept time,
    # never recomputed from a later offset.
    assert event.clock_offset_ms == 0
    assert event.payload["clock_offset_version"] == 1
    assert event.payload["normalized_server_timestamp_ms"] == 1234


def test_duplicate_event_is_persisted_only_once():
    session_id = _fresh_session_id()
    event = {
        "sequence": 1,
        "client_timestamp_ms": 0,
        "event_type": "observer_note",
        "payload": {"event_id": "evt-dup", "note": "hello"},
    }
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(event)
        assert wizard.receive_json()["status"] == "accepted"

        wizard.send_json(event)
        assert wizard.receive_json()["status"] == "duplicate"

        # The in-memory dedup set short-circuits before the database is ever
        # consulted, so drop it to exercise the ADR-0001 §7 uniqueness keys the
        # way a server restart would: memory empty, row still there.
        websocket_module.manager._processed_event_ids[session_id].clear()
        wizard.send_json(event)
        assert wizard.receive_json()["status"] == "duplicate"

    assert len(_stored_events(session_id)) == 1


def test_rejected_event_is_not_persisted():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "clip_command",
                "payload": {
                    "command_id": "cmd-expired",
                    "expires_at_ms": 1,  # already in the past
                    "snapshot_revision": 0,
                    "clip_id": "C001",
                },
            }
        )
        assert wizard.receive_json()["reason"] == "command_expired"

    assert _stored_events(session_id) == []


def test_session_lifecycle_events_are_mirrored_onto_the_session_row():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "evt-start"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["type"] == "session_start_scheduled"

        # ADR-0003 §5: the row holds the unified start time and stays in
        # `starting` until that monotonic instant is reached.
        row = _session_row(session_id)
        assert row.status == "starting"
        assert row.snapshot_revision == 1
        assert row.start_at_ms is not None

        wizard.send_json(
            {
                "sequence": 2,
                "client_timestamp_ms": 0,
                "event_type": "session_paused",
                "payload": {
                    "event_id": "evt-pause",
                    "command_id": "evt-pause",
                    "expires_at_ms": _far_future_ms(),
                    "snapshot_revision": 1,
                    "reason": "recording_failure",
                },
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        row = _session_row(session_id)
        assert row.status == "paused"
        assert row.pause_reason == "recording_failure"
        assert row.snapshot_revision == 2

        wizard.send_json(
            {
                "sequence": 3,
                "client_timestamp_ms": 0,
                "event_type": "session_resumed",
                "payload": {"event_id": "evt-resume"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        row = _session_row(session_id)
        assert row.status == "active"
        assert row.pause_reason is None
        assert row.snapshot_revision == 3

        wizard.send_json(
            {
                "sequence": 4,
                "client_timestamp_ms": 0,
                "event_type": "session_ended",
                "payload": {"event_id": "evt-end"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        row = _session_row(session_id)
        assert row.status == "ended"
        assert row.ended_at is not None
        assert row.snapshot_revision == 4

        # The in-memory snapshot never fell behind the row (ADR-0004 §1).
        assert websocket_module.manager.snapshot_for(session_id).snapshot_revision == 4

    # `models.pause_session` audits who paused, with the socket's URL role.
    paused_audits = _audit_rows(session_id, models.AUDIT_SESSION_PAUSED)
    assert len(paused_audits) == 1
    assert paused_audits[0].actor_role == "wizard"
    assert paused_audits[0].details["reason"] == "recording_failure"


def test_scheduled_start_coming_due_marks_the_row_active():
    """ADR-0003 §5's deferred `starting -> active` reaches the session row.

    Also pins the deliberate asymmetry documented in the implementation notes:
    this transition bumps the row's revision but *not* the in-memory one,
    because no client was ever told about it.
    """
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "evt-start"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["type"] == "session_start_scheduled"
        assert _session_row(session_id).status == "starting"

        # Pull the scheduled monotonic instant into the past so the next
        # message settles it, instead of waiting 2 real seconds.
        snapshot = websocket_module.manager.snapshot_for(session_id)
        snapshot.start_at_server_ms = 1

        wizard.send_json(
            {
                "type": "reconnect_hello",
                "session_id": session_id,
                "role": "wizard",
                "last_snapshot_revision": 1,
                "last_acked_sequence": 0,
                "pending_event_ids": [],
            }
        )
        served = wizard.receive_json()
        assert served["session_state"] == "active"
        assert "start_at_server_ms" not in served

    row = _session_row(session_id)
    assert row.status == "active"
    assert row.snapshot_revision == 2
    # The in-memory revision is deliberately left behind: bumping it for a
    # transition nobody was told about would make in-flight commands look stale.
    assert websocket_module.manager.snapshot_for(session_id).snapshot_revision == 1


def test_pause_during_the_scheduled_start_window_is_not_overridden():
    """A pause inside the 2s window clears the pending start, so it stands."""
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_started",
                "payload": {"event_id": "evt-start"},
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["type"] == "session_start_scheduled"

        wizard.send_json(
            {
                "sequence": 2,
                "client_timestamp_ms": 0,
                "event_type": "session_paused",
                "payload": {
                    "event_id": "evt-pause-in-window",
                    "command_id": "evt-pause-in-window",
                    "expires_at_ms": _far_future_ms(),
                    "snapshot_revision": 1,
                    "reason": "clock_sync_failure",
                },
            }
        )
        assert wizard.receive_json()["status"] == "accepted"

        # Even once the original start instant has passed, the pause holds:
        # `_apply_snapshot_update` cleared `start_at_server_ms`, so there is no
        # scheduled start left to resolve.
        wizard.send_json({"type": "ping", "ping_id": "past-window", "client_timestamp_ms": 0})
        assert wizard.receive_json()["ping_id"] == "past-window"

    assert websocket_module.manager.snapshot_for(session_id).session_state == "paused"
    row = _session_row(session_id)
    assert row.status == "paused"
    assert row.pause_reason == "clock_sync_failure"


def test_lifecycle_event_the_row_cannot_accept_is_rejected_and_not_persisted():
    """A lifecycle event must not be accepted unless SQLite can mirror it."""
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "session_paused",
                "payload": {
                    "event_id": "evt-early-pause",
                    "command_id": "evt-early-pause",
                    "expires_at_ms": _far_future_ms(),
                    "snapshot_revision": 0,
                    "reason": "manual",
                },
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "invalid_lifecycle_transition"

        # The socket stays usable afterwards.
        wizard.send_json({"type": "ping", "ping_id": "after-skip", "client_timestamp_ms": 0})
        assert wizard.receive_json()["ping_id"] == "after-skip"

    assert _stored_events(session_id) == []
    row = _session_row(session_id)
    assert row.status == "created"
    assert row.snapshot_revision == 0


def test_connection_takeover_is_recorded_in_the_audit_log():
    session_id = _fresh_session_id()
    with _connect(session_id, "teacher") as teacher:
        # Opened without a `with` block: the server closes this socket during
        # the takeover, so its teardown is handled explicitly below.
        stale_student = _connect(session_id, "student")
        stale_student.__enter__()
        try:
            _mark_connection_stale(session_id, "student")
            with _connect(session_id, "student"):
                notice = teacher.receive_json()
                assert notice["type"] == "role_connection_replaced"
                assert notice["role"] == "student"
        finally:
            try:
                stale_student.__exit__(None, None, None)
            except Exception:
                pass  # already closed server-side by the takeover

    takeovers = _audit_rows(session_id, models.AUDIT_CONNECTION_TAKEOVER)
    assert len(takeovers) == 1
    assert takeovers[0].actor_role == "student"
    assert takeovers[0].session_id == session_id
    # ADR-0004 §3: no raw token, and nothing token-shaped in the details.
    assert takeovers[0].token_hash is None
    assert takeovers[0].details["replaced_role"] == "student"


# --- ADR-0002 fallback resolution ------------------------------------------

#: The repo's real case library. `cases.CASES_DIR` defaults to `./cases`, which
#: does not exist relative to `server/` where pytest runs, so resolution would
#: otherwise always degrade to the generic clips.
REPO_CASES_DIR = Path(__file__).resolve().parents[2] / "cases"


@pytest.fixture
def repo_cases(monkeypatch):
    """Point the case loader at the repo's real `cases/` directory."""
    monkeypatch.setattr(cases, "CASES_DIR", REPO_CASES_DIR)
    return REPO_CASES_DIR


def _clip(
    clip_id: str,
    *,
    intent: str,
    character_state: str = "neutral",
    category: str = "fallback",
    forbidden_after: tuple[str, ...] = (),
) -> dict:
    """One synthetic `clip_manifest.json` entry (spec §12)."""
    return {
        "clip_id": clip_id,
        "category": category,
        "intent": intent,
        "character_state": character_state,
        "text": "…",
        "reveals": [],
        "requires": [],
        "forbidden_after": list(forbidden_after),
        "duration_ms": 1000,
        "file": f"media/{clip_id}.mp4",
    }


def _install_case(monkeypatch, case_id: str, clips: list[dict]) -> str:
    """Serve a synthetic clip manifest for `case_id` from memory.

    CASE001 ships exactly one fallback clip, so the multi-candidate rules
    (`forbidden_after`, character state, avoid-immediate-repeat) need a case of
    their own rather than an edit to another role's `cases/` directory. The
    manifest is injected at the loader seam -- parsing the JSON off disk is
    already covered by the CASE001 tests, and this keeps the suite independent
    of a writable temp directory.
    """
    manifest = [ClipManifestEntry.model_validate(c) for c in clips]

    def _fake_load(requested_case_id: str):
        if requested_case_id != case_id:
            raise FileNotFoundError(f"case not found: {requested_case_id}")
        return list(manifest)

    monkeypatch.setattr(cases, "load_clip_manifest", _fake_load)
    return case_id


def test_resolve_fallback_clip_falls_back_to_the_generic_clips(monkeypatch):
    case_id = _install_case(monkeypatch, "CASE_EMPTY", [])
    for category in cases.FALLBACK_CATEGORIES:
        resolved = cases.resolve_fallback_clip(case_id, category)
        assert resolved == cases.GENERIC_FALLBACK_CLIPS[category]

    # An unreadable case must degrade the same way rather than raise into the
    # realtime path.
    for category in cases.FALLBACK_CATEGORIES:
        assert cases.resolve_fallback_clip("CASE_MISSING", category) == (
            cases.GENERIC_FALLBACK_CLIPS[category]
        )


def test_resolve_fallback_clip_prefers_the_case_manifest_without_changing_category(
    repo_cases,
):
    assert cases.resolve_fallback_clip("CASE001", "CLARIFY") == (
        "C001_FALLBACK_CLARIFY_01"
    )
    # CASE001 has no approved clip for the other three categories; ADR-0002 §4
    # forbids answering with a different category, so the generic clip stands.
    assert cases.resolve_fallback_clip("CASE001", "UNKNOWN_OR_UNSURE") == (
        "GEN_UNKNOWN_01"
    )


def test_resolve_fallback_clip_skips_clips_forbidden_by_revealed_facts(monkeypatch):
    case_id = _install_case(
        monkeypatch,
        "CASE_FORBID",
        [
            _clip("CX_CLARIFY_01", intent="ask_clarify", forbidden_after=("alibi_broken",)),
            _clip("CX_CLARIFY_02", intent="ask_clarify"),
        ],
    )
    assert cases.resolve_fallback_clip(case_id, "CLARIFY") == "CX_CLARIFY_01"
    assert cases.resolve_fallback_clip(
        case_id, "CLARIFY", revealed_facts=["alibi_broken"]
    ) == "CX_CLARIFY_02"


def test_resolve_fallback_clip_prefers_the_current_character_state(monkeypatch):
    case_id = _install_case(
        monkeypatch,
        "CASE_STATE",
        [
            _clip("CX_CLARIFY_CALM", intent="ask_clarify", character_state="calm"),
            _clip("CX_CLARIFY_DEF", intent="ask_clarify", character_state="defensive"),
        ],
    )
    assert cases.resolve_fallback_clip(
        case_id, "CLARIFY", character_state="defensive"
    ) == "CX_CLARIFY_DEF"
    # No clip for this state: keep the category, take the first candidate.
    assert cases.resolve_fallback_clip(
        case_id, "CLARIFY", character_state="furious"
    ) == "CX_CLARIFY_CALM"


def test_resolve_fallback_clip_avoids_an_immediate_repeat_within_the_category(
    monkeypatch,
):
    case_id = _install_case(
        monkeypatch,
        "CASE_REPEAT",
        [
            _clip("CX_CLARIFY_01", intent="ask_clarify"),
            _clip("CX_CLARIFY_02", intent="ask_clarify"),
            _clip("CX_DECLINE_01", intent="decline_answer"),
        ],
    )
    assert cases.resolve_fallback_clip(case_id, "CLARIFY") == "CX_CLARIFY_01"
    assert cases.resolve_fallback_clip(
        case_id, "CLARIFY", last_fallback_clip_id="CX_CLARIFY_01"
    ) == "CX_CLARIFY_02"
    # The only approved clip for a category repeats rather than switching to
    # one that says something else (ADR-0002 §4).
    assert cases.resolve_fallback_clip(
        case_id, "DECLINE_OR_BOUNDARY", last_fallback_clip_id="CX_DECLINE_01"
    ) == "CX_DECLINE_01"


def test_resolve_fallback_clip_rejects_a_category_outside_the_fixed_four():
    with pytest.raises(ValueError):
        cases.resolve_fallback_clip("CASE001", "PRESSURE_REACTION")


def test_is_fallback_clip_separates_case_answers_from_fallback_material(repo_cases):
    assert cases.is_fallback_clip("CASE001", "C001_FALLBACK_CLARIFY_01") is True
    assert cases.is_fallback_clip("CASE001", "C001_TIMELINE_LEAVE_02") is False
    assert cases.is_fallback_clip("CASE001", "GEN_UNKNOWN_01") is True


# --- ADR-0002 fallback over the WebSocket ----------------------------------


def _fallback_request(
    session_id: str,
    *,
    sequence: int,
    category: str,
    command_id: str,
    reason: str | None = None,
) -> dict:
    """A Wizard `request_fallback` control message (ADR-0002 §2).

    `snapshot_revision` is read from the live snapshot because every accepted
    fallback bumps it (ADR-0004 §1) -- a Wizard client tracks the same value
    from the last snapshot/ACK it saw.
    """
    payload = {
        "command_id": command_id,
        "expires_at_ms": _far_future_ms(),
        "snapshot_revision": websocket_module.manager.snapshot_for(
            session_id
        ).snapshot_revision,
        "fallback_category": category,
    }
    if reason is not None:
        payload["reason"] = reason
    return {
        "type": "request_fallback",
        "sequence": sequence,
        "client_timestamp_ms": sequence * 1000,
        "payload": payload,
    }


def _start_session(sock, *, sequence: int = 1) -> None:
    """Drive an accepted `session_started` and drain its two replies."""
    sock.send_json(
        {
            "sequence": sequence,
            "client_timestamp_ms": 0,
            "event_type": "session_started",
            "payload": {"event_id": f"evt-start-{sequence}"},
        }
    )
    assert sock.receive_json()["status"] == "accepted"
    assert sock.receive_json()["type"] == "session_start_scheduled"


def test_request_fallback_resolves_each_category_and_broadcasts_fallback_used(
    repo_cases,
):
    session_id = _fresh_session_id()
    expected = {
        "CLARIFY": "C001_FALLBACK_CLARIFY_01",
        "ONE_AT_A_TIME": "GEN_ONE_AT_A_TIME_01",
        "UNKNOWN_OR_UNSURE": "GEN_UNKNOWN_01",
        "DECLINE_OR_BOUNDARY": "GEN_DECLINE_01",
    }
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        for index, (category, clip_id) in enumerate(expected.items(), start=1):
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

            # Broadcast to *both* roles: the student plays the clip, and the
            # Wizard learns which clip the server resolved (ADR-0002 §1).
            for sock in (wizard, student):
                used = sock.receive_json()
                assert used["event_type"] == "fallback_used"
                assert used["source"] == "wizard"
                assert used["payload"]["fallback_category"] == category
                assert used["payload"]["resolved_clip_id"] == clip_id
                assert used["payload"]["wizard_selected"] is True
                assert used["payload"]["reason"] == "ambiguous_question"

            if index >= FALLBACK_THRESHOLD:
                # The streak alert has its own test; drain it so the next
                # iteration reads the next fallback.
                for sock in (wizard, student):
                    assert sock.receive_json()["type"] == "fallback_threshold_reached"

        assert websocket_module.manager.snapshot_for(session_id).active_clip == (
            "GEN_DECLINE_01"
        )

    fallback_events = [
        e for e in _stored_events(session_id) if e.event_type == "fallback_used"
    ]
    assert len(fallback_events) == len(expected)
    assert [e.payload["resolved_clip_id"] for e in fallback_events] == list(
        expected.values()
    )


def test_consecutive_fallbacks_alert_at_three_and_reset_on_normal_playback(repo_cases):
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        for index in (1, 2):
            wizard.send_json(
                _fallback_request(
                    session_id,
                    sequence=index,
                    category="CLARIFY",
                    command_id=f"cmd-{index}",
                )
            )
            assert wizard.receive_json()["status"] == "accepted"
            used = wizard.receive_json()
            assert used["payload"]["consecutive_fallbacks"] == index

        wizard.send_json(
            _fallback_request(
                session_id, sequence=3, category="CLARIFY", command_id="cmd-3"
            )
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["payload"]["consecutive_fallbacks"] == 3
        alert = wizard.receive_json()
        assert alert["type"] == "fallback_threshold_reached"
        assert alert["session_id"] == session_id
        assert alert["payload"] == {"consecutive_fallbacks": FALLBACK_THRESHOLD}

        # The count is part of the snapshot, so a reconnecting Wizard restores
        # its warning state.
        wizard.send_json(
            {
                "type": "reconnect_hello",
                "session_id": session_id,
                "role": "wizard",
                "last_snapshot_revision": 0,
                "last_acked_sequence": 0,
                "pending_event_ids": [],
            }
        )
        assert wizard.receive_json()["consecutive_fallbacks"] == 3

        # A real case answer ends the streak (ADR-0002 §5).
        wizard.send_json(
            {
                "sequence": 4,
                "client_timestamp_ms": 4000,
                "event_type": "clip_command",
                "payload": {
                    "command_id": "cmd-clip",
                    "expires_at_ms": _far_future_ms(),
                    "snapshot_revision": websocket_module.manager.snapshot_for(
                        session_id
                    ).snapshot_revision,
                    "clip_id": "C001_TIMELINE_LEAVE_02",
                },
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert websocket_module.manager.snapshot_for(session_id).consecutive_fallbacks == 0

        # The next fallback starts a fresh streak, and raises no alert.
        wizard.send_json(
            _fallback_request(
                session_id, sequence=5, category="CLARIFY", command_id="cmd-5"
            )
        )
        assert wizard.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["payload"]["consecutive_fallbacks"] == 1

        wizard.send_json(
            {"type": "ping", "ping_id": "no-alert", "client_timestamp_ms": 0}
        )
        assert wizard.receive_json()["ping_id"] == "no-alert"


def test_playing_the_fallback_clip_itself_does_not_reset_the_streak(repo_cases):
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        wizard.send_json(
            _fallback_request(
                session_id, sequence=1, category="CLARIFY", command_id="cmd-1"
            )
        )
        assert wizard.receive_json()["status"] == "accepted"
        used = student.receive_json()
        resolved_clip_id = used["payload"]["resolved_clip_id"]
        assert wizard.receive_json()["event_type"] == "fallback_used"

        # The student reports the resolved fallback clip starting; that is the
        # fallback itself, not a case answer, so the streak stands.
        student.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 1000,
                "event_type": "clip_started",
                "payload": {"event_id": "evt-clip-started", "clip_id": resolved_clip_id},
            }
        )
        assert student.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["event_type"] == "clip_started"

    assert websocket_module.manager.snapshot_for(session_id).consecutive_fallbacks == 1


def test_repeated_request_fallback_is_acked_duplicate_without_advancing_the_streak(
    repo_cases,
):
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        request = _fallback_request(
            session_id, sequence=1, category="CLARIFY", command_id="cmd-retry"
        )
        wizard.send_json(request)
        assert wizard.receive_json()["status"] == "accepted"
        assert wizard.receive_json()["event_type"] == "fallback_used"

        # A retry after a missed ACK still carries the pre-fallback snapshot
        # revision, so de-duplication has to win over the staleness check.
        wizard.send_json(request)
        assert wizard.receive_json() == {
            "type": "event_ack",
            "event_id": "cmd-retry",
            "status": "duplicate",
            "reason": None,
        }

        wizard.send_json(
            {"type": "ping", "ping_id": "no-second-broadcast", "client_timestamp_ms": 0}
        )
        assert wizard.receive_json()["ping_id"] == "no-second-broadcast"

    assert websocket_module.manager.snapshot_for(session_id).consecutive_fallbacks == 1
    assert len([e for e in _stored_events(session_id) if e.event_type == "fallback_used"]) == 1


def test_request_fallback_rejects_unknown_categories_and_non_wizard_roles(repo_cases):
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        wizard.send_json(
            _fallback_request(
                session_id,
                sequence=1,
                category="PRESSURE_REACTION",
                command_id="cmd-bad-category",
            )
        )
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "invalid_fallback_category"

        student.send_json(
            _fallback_request(
                session_id, sequence=1, category="CLARIFY", command_id="cmd-student"
            )
        )
        ack = student.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "fallback_requires_wizard"

    assert _stored_events(session_id) == []


def test_request_fallback_without_envelope_fields_is_rejected_with_its_command_id(
    repo_cases,
):
    """A rejection the Wizard cannot correlate is as bad as no rejection."""
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        request = _fallback_request(
            session_id, sequence=1, category="CLARIFY", command_id="cmd-no-sequence"
        )
        del request["sequence"]
        wizard.send_json(request)
        assert wizard.receive_json() == {
            "type": "event_ack",
            "event_id": "cmd-no-sequence",
            "status": "rejected",
            "reason": "invalid_envelope_fields",
        }

    assert _stored_events(session_id) == []


def test_request_fallback_is_rejected_while_the_session_is_paused(repo_cases):
    """ADR-0002 §8: a technical pause blocks new Wizard answers."""
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        _start_session(wizard)
        wizard.send_json(
            {
                "sequence": 2,
                "client_timestamp_ms": 0,
                "event_type": "session_paused",
                "payload": {
                    "event_id": "evt-pause",
                    "command_id": "evt-pause",
                    "expires_at_ms": _far_future_ms(),
                    "snapshot_revision": websocket_module.manager.snapshot_for(
                        session_id
                    ).snapshot_revision,
                    "reason": "network_failure",
                },
            }
        )
        assert wizard.receive_json()["status"] == "accepted"

        wizard.send_json(
            _fallback_request(
                session_id, sequence=3, category="CLARIFY", command_id="cmd-paused"
            )
        )
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "session_paused"

    assert [e for e in _stored_events(session_id) if e.event_type == "fallback_used"] == []


def test_client_authored_fallback_used_event_is_rejected():
    """Only the server decides which clip a category resolves to."""
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "fallback_used",
                "payload": {
                    "event_id": "evt-self-served",
                    "fallback_category": "CLARIFY",
                    "resolved_clip_id": "C001_TIMELINE_LEAVE_02",
                },
            }
        )
        ack = wizard.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "fallback_is_server_resolved"

    assert _stored_events(session_id) == []


# --- ADR-0002 §7 uncovered questions ---------------------------------------


def test_uncovered_question_is_persisted_and_broadcast_to_review_roles_only():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student, \
            _connect(session_id, "teacher") as teacher, \
            _connect(session_id, "observer") as observer:
        wizard.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 12_000,
                "event_type": "uncovered_question",
                "payload": {
                    "event_id": "evt-uncovered",
                    "question_segment_id": "SEG-041",
                    "note": "asked about the second phone",
                },
            }
        )
        assert wizard.receive_json()["status"] == "accepted"
        teacher_forwarded = teacher.receive_json()
        observer_forwarded = observer.receive_json()
        assert teacher_forwarded["event_type"] == "uncovered_question"
        assert teacher_forwarded["payload"]["question_segment_id"] == "SEG-041"
        assert observer_forwarded["event_type"] == "uncovered_question"
        assert observer_forwarded["payload"]["question_segment_id"] == "SEG-041"

        student.send_json(
            {"type": "ping", "ping_id": "no-uncovered-question", "client_timestamp_ms": 0}
        )
        assert student.receive_json()["ping_id"] == "no-uncovered-question"

    stored = _stored_events(session_id)
    assert len(stored) == 1
    assert stored[0].event_type == "uncovered_question"
    assert stored[0].source == "wizard"
    assert stored[0].payload["note"] == "asked about the second phone"


def test_uncovered_question_from_the_student_is_rejected():
    session_id = _fresh_session_id()
    with _connect(session_id, "student") as student:
        student.send_json(
            {
                "sequence": 1,
                "client_timestamp_ms": 0,
                "event_type": "uncovered_question",
                "payload": {"event_id": "evt-student-uncovered"},
            }
        )
        ack = student.receive_json()
        assert ack["status"] == "rejected"
        assert ack["reason"] == "uncovered_question_requires_wizard"

    assert _stored_events(session_id) == []


# --- ADR-0002 §8 disconnect staging / technical pause -----------------------


def _make_start_due(session_id: str) -> None:
    """Pull ADR-0003's scheduled start into the past so the session is active."""
    websocket_module.manager.snapshot_for(session_id).start_at_server_ms = 1


def _age_disconnect_mark(session_id: str, role: str) -> None:
    """Age a recorded disconnect past DISCONNECT_GRACE_MS.

    The alternative is sleeping through the ADR-0002 §8 grace window in real
    time; this reaches into the same tracked state `lost_critical_roles` reads.
    """
    marks = websocket_module.manager._disconnected_since[session_id]
    assert role in marks, f"the {role} socket close was never registered"
    marks[role] -= DISCONNECT_GRACE_MS + 1_000


def _wait_for_session_pause(session_id: str, *, timeout_s: float = 1.0) -> None:
    """Poll the row until an async disconnect task has had a chance to run."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _session_row(session_id).status == "paused":
            return
        time.sleep(0.01)
    assert _session_row(session_id).status == "paused"


def _assert_network_failure_pause(session_id: str, lost_role: str) -> None:
    row = _session_row(session_id)
    assert row.status == "paused"
    assert row.pause_reason == "network_failure"

    # ADR-0004 §3: the automatic pause is attributed to the server, not to a
    # human actor who did not do it.
    audits = _audit_rows(session_id, models.AUDIT_SESSION_PAUSED)
    assert [a.actor_role for a in audits] == ["server"]
    assert audits[0].details["reason"] == "network_failure"

    paused_events = [
        e for e in _stored_events(session_id) if e.event_type == "session_paused"
    ]
    assert len(paused_events) == 1
    assert paused_events[0].source == "server"
    assert paused_events[0].payload["lost_roles"] == [lost_role]


def test_stale_heartbeat_on_a_critical_role_pauses_for_network_failure():
    """A socket that is still open but silent past the heartbeat threshold."""
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, _connect(session_id, "student"):
        _start_session(wizard)
        _make_start_due(session_id)
        _mark_connection_stale(session_id, "student")

        wizard.send_json({"type": "ping", "ping_id": "tick", "client_timestamp_ms": 0})
        paused = wizard.receive_json()
        assert paused["event_type"] == "session_paused"
        assert paused["source"] == "server"
        assert paused["payload"]["reason"] == "network_failure"
        assert paused["payload"]["lost_roles"] == ["student"]
        assert paused["payload"]["auto_paused"] is True
        # The pause is evaluated before the message that triggered it is served.
        assert wizard.receive_json()["type"] == "pong"

    _assert_network_failure_pause(session_id, "student")


def test_non_ping_traffic_keeps_a_busy_critical_role_from_being_declared_lost(
    repo_cases,
):
    """Work counts as a heartbeat: silence is what ADR-0001 §2 measures.

    Otherwise a Wizard whose pings lapse while it keeps issuing commands would
    pause the session with its own message, and then be refused by the pause it
    just caused.
    """
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard, \
            _connect(session_id, "student") as student:
        _start_session(wizard)
        # The student saw the same start: drain its copies so the next read is
        # the fallback broadcast.
        assert student.receive_json()["type"] == "session_start_scheduled"
        assert student.receive_json()["event_type"] == "session_started"
        _make_start_due(session_id)
        _mark_connection_stale(session_id, "wizard")

        wizard.send_json(
            _fallback_request(
                session_id, sequence=2, category="CLARIFY", command_id="cmd-busy"
            )
        )
        ack = wizard.receive_json()
        assert ack["status"] == "accepted"
        assert ack["event_id"] == "cmd-busy"
        for sock in (wizard, student):
            assert sock.receive_json()["event_type"] == "fallback_used"

    assert _session_row(session_id).status != "paused"
    assert _audit_rows(session_id, models.AUDIT_SESSION_PAUSED) == []


def test_critical_role_disconnect_pauses_only_after_the_grace_window():
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        student = _connect(session_id, "student")
        student.__enter__()
        _start_session(wizard)
        _make_start_due(session_id)
        student.__exit__(None, None, None)

        # ADR-0002 §8 tier 1: a blip under 2 seconds is smoothed over -- the
        # ping is answered and nothing is paused.
        wizard.send_json(
            {"type": "ping", "ping_id": "inside-grace", "client_timestamp_ms": 0}
        )
        assert wizard.receive_json()["ping_id"] == "inside-grace"
        assert _session_row(session_id).status != "paused"

        _age_disconnect_mark(session_id, "student")
        wizard.send_json(
            {"type": "ping", "ping_id": "past-grace", "client_timestamp_ms": 0}
        )
        paused = wizard.receive_json()
        assert paused["event_type"] == "session_paused"
        assert paused["payload"]["reason"] == "network_failure"
        assert wizard.receive_json()["ping_id"] == "past-grace"

        # Already paused: the check short-circuits instead of pausing twice.
        wizard.send_json(
            {"type": "ping", "ping_id": "still-paused", "client_timestamp_ms": 0}
        )
        assert wizard.receive_json()["ping_id"] == "still-paused"

    _assert_network_failure_pause(session_id, "student")
    assert websocket_module.manager.snapshot_for(session_id).session_state == "paused"


def test_critical_role_disconnect_pauses_without_later_inbound_traffic(monkeypatch):
    session_id = _fresh_session_id()
    monkeypatch.setattr(websocket_module, "DISCONNECT_GRACE_MS", 10)

    with _connect(session_id, "wizard") as wizard:
        student = _connect(session_id, "student")
        student.__enter__()
        _start_session(wizard)
        _make_start_due(session_id)
        student.__exit__(None, None, None)

        _wait_for_session_pause(session_id)
        paused = wizard.receive_json()
        assert paused["event_type"] == "session_paused"
        assert paused["payload"]["reason"] == "network_failure"
        assert paused["payload"]["lost_roles"] == ["student"]

    _assert_network_failure_pause(session_id, "student")


def test_disconnect_does_not_pause_a_session_that_never_started():
    """No interview is underway, so there is nothing to technically pause."""
    session_id = _fresh_session_id()
    with _connect(session_id, "wizard") as wizard:
        student = _connect(session_id, "student")
        student.__enter__()
        student.__exit__(None, None, None)
        _age_disconnect_mark(session_id, "student")

        wizard.send_json({"type": "ping", "ping_id": "idle", "client_timestamp_ms": 0})
        assert wizard.receive_json()["ping_id"] == "idle"

    row = _session_row(session_id)
    assert row.status == "created"
    assert row.pause_reason is None
    assert _audit_rows(session_id, models.AUDIT_SESSION_PAUSED) == []
