"""WebSocket endpoint + connection manager (spec §4, §9; ADR-0001; ADR-0004).

Wizard and student clients hold a persistent WebSocket to the server for
real-time commands (clip play/stop, session control) and event streaming.

This module implements the ADR-0001 "Accepted" decisions:
  - Application-layer heartbeat (10s ping / 5s pong-wait / 3 missed -> stale).
  - Snapshot-first reconnect coordination (`reconnect_hello` / `session_snapshot`).
  - Event ACK with idempotent de-duplication (`accepted` / `duplicate` / `rejected`).
  - Short-lived command validity checks (`command_id`, `expires_at_ms`,
    `snapshot_revision`) so stale commands are never forwarded after a
    reconnect (ADR-0001 §5, ADR-0004 §1).
  - Single-connection-per-role constraints for `student`/`wizard`, with
    `role_connection_replaced` notification when a stale connection is
    replaced (ADR-0001 §9).

Time-sync (ADR-0003) is implemented below:
  - Bidirectional probe/response for offset+RTT sampling
    (`clock_sync_probe` / `clock_sync_response`), using server monotonic
    time (`_monotonic_ms`) for `s1`/`s2`.
  - `compute_offset_median` -- pure statistical helper implementing the
    initial-9/keep-5 and continuous-5/keep-3 lowest-RTT median filters
    (ADR-0003 §4, §6). Sample filtering/reporting is client-driven; the
    server only consumes the client's final `clock_offset_report`.
  - Per-connection `clock_offset_ms` / `clock_offset_version` tracking, and
    injection of `clock_offset_ms` (when the sender omits it) plus
    `clock_offset_version` / `normalized_server_timestamp_ms` into accepted
    event payloads. Historical events are never recalculated or rewritten
    (ADR-0003 §7).
  - `session_start_scheduled` broadcast (`start_at_server_ms` = server
    monotonic time + 2000ms) on `session_started` (ADR-0003 §5), with the
    snapshot held in a non-active pending-start state until that monotonic
    time is reached.

Persistence + audit wiring (ADR-0004 §1-§4) is implemented on top of
``models.py``'s access layer -- this module only calls its public functions and
never touches the schema itself:
  - The upgrade refuses a `session_id` with no persisted row (close 4404).
  - Every accepted envelope is written through ``models.append_event`` *before*
    it is ACKed, so an ``accepted`` always means "durable" (ADR-0004 §4).
  - Accepted lifecycle events mirror onto the session row via
    ``start_session`` / ``pause_session`` / ``resume_session`` /
    ``end_session``, keeping ``snapshot_revision`` monotonic across both. A
    lifecycle event the row cannot apply is rejected before ACK.
  - A single-connection-role takeover writes a ``connection_takeover`` audit
    row (ADR-0004 §3).

Fallback + disconnect UX (ADR-0002) is layered on top of that:
  - `request_fallback` is a Wizard *control message* (`type` /
    `command_type`), because the Wizard sends a semantic category and the
    server -- not the client -- resolves the approved clip out of the case
    manifest via `cases.resolve_fallback_clip` (ADR-0002 §1, §2). The result
    is recorded as a normal `fallback_used` envelope (ADR-0002 §7).
  - `consecutive_fallbacks` is tracked on the snapshot, reset when a
    non-fallback clip starts playing, and announced with a
    `fallback_threshold_reached` control message at 3 (ADR-0002 §5).
  - A critical role (student/wizard) that stays disconnected past
    `DISCONNECT_GRACE_MS`, or whose socket goes silent past
    `HEARTBEAT_STALE_MS`, pauses the session with `network_failure`
    (ADR-0002 §8, ADR-0004 §2).

The pre-existing envelope-based `ping`/`pong`/`clock_sync` event types are
left untouched and do not overlap with the application-layer heartbeat
(distinguished by the `type` vs `event_type` top-level key, see
`_handle_message`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DBSession

from . import cases, models
from .events import EventEnvelope, EventType, Source

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Heartbeat tuning (ADR-0001 §2) ----------------------------------------
PING_INTERVAL_MS = 10_000
PONG_WAIT_MS = 5_000
MAX_MISSED_HEARTBEATS = 3
# Server-side staleness threshold used to decide whether a tracked connection
# for a single-connection role (student/wizard) can be replaced by a new one
# without an explicit takeover confirmation. ADR-0001 §2 puts the *client's*
# offline determination at "約 25-30 秒"; we reuse that window here. It is also
# the "心跳失聯門檻" half of the ADR-0002 §8 technical pause: a socket that is
# still open but has missed this many ms of heartbeats counts as lost.
HEARTBEAT_STALE_MS = 30_000

# --- Fallback + disconnect UX tuning (ADR-0002) -----------------------------
# ADR-0002 §8 tier 2: an interruption under 2 seconds is smoothed over, but
# past it the session must stop counting interview time and say so. A closed
# socket is already a hard signal, so this is measured from the disconnect
# rather than from the last heartbeat.
DISCONNECT_GRACE_MS = 2_000
# ADR-0002 §5: at three consecutive fallbacks the system recommends the host
# step in, instead of letting fallbacks paper over a stuck interview.
FALLBACK_THRESHOLD = 3

SINGLE_CONNECTION_ROLES = {"student", "wizard"}
MULTI_CONNECTION_ROLES = {"teacher", "observer"}
VALID_ROLES = SINGLE_CONNECTION_ROLES | MULTI_CONNECTION_ROLES
EVENT_SOURCE_ROLES = {source.value for source in Source}
PAUSE_REASONS = {
    "manual",
    "network_failure",
    "clock_sync_failure",
    "recording_failure",
    "ethical_or_safety_stop",
}

WS_SESSION_COOKIE = "mindprobe_session_token"
WS_TOKEN_SECRET_ENV = "MINDPROBE_WS_TOKEN_SECRET"
WS_TOKEN_TTL_SECONDS = 2 * 60 * 60

# Business events with a bounded validity window (ADR-0001 §5). These must
# carry command_id/expires_at_ms/snapshot_revision in `payload` and are
# discarded -- never forwarded -- once expired or superseded by a newer
# snapshot revision.
SHORT_LIVED_COMMAND_TYPES = {
    EventType.CLIP_COMMAND,
    EventType.RETURN_TO_IDLE,
    EventType.SESSION_PAUSED,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _monotonic_ms() -> int:
    """Server monotonic time in ms (ADR-0003 §1).

    Used for all relative-time calculations (RTT/offset sampling, scheduled
    start, command expiry) since it is immune to wall-clock adjustments;
    `_now_ms()` remains reserved for audit/log/administrative timestamps.
    """
    return time.monotonic_ns() // 1_000_000


def compute_offset_median(samples: list[dict], keep_lowest_rtt_count: int) -> float:
    """Median clock offset from the lowest-RTT subset of `samples` (ADR-0003 §4, §6).

    Each sample is a dict with integer `t0` (client send), `s1` (server
    receive), `s2` (server send), `t3` (client receive), all in ms. Samples
    are sorted by RTT ascending, the lowest `keep_lowest_rtt_count` are kept,
    and the median of their `offset` values is returned:

        RTT = (t3 - t0) - (s2 - s1)
        offset = ((s1 - t0) + (s2 - t3)) / 2

    Callers use 9 samples / keep 5 for initial sync and 5 samples / keep 3
    for continuous drift correction.
    """
    scored: list[tuple[int, float]] = []
    for sample in samples:
        t0, s1, s2, t3 = sample["t0"], sample["s1"], sample["s2"], sample["t3"]
        rtt = (t3 - t0) - (s2 - s1)
        offset = ((s1 - t0) + (s2 - t3)) / 2
        scored.append((rtt, offset))
    scored.sort(key=lambda pair: pair[0])
    kept_offsets = sorted(offset for _, offset in scored[:keep_lowest_rtt_count])
    n = len(kept_offsets)
    mid = n // 2
    if n % 2 == 1:
        return kept_offsets[mid]
    return (kept_offsets[mid - 1] + kept_offsets[mid]) / 2


class RoleConnectionConflict(Exception):
    """Raised when a single-connection role already has a live connection."""

    def __init__(self, role: str) -> None:
        super().__init__(f"role already connected: {role}")
        self.role = role


class WebSocketAuthError(Exception):
    """Raised when the WebSocket upgrade token is missing or invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ConnectionInfo:
    websocket: WebSocket
    connected_at_ms: int
    last_heartbeat_ms: int
    event_loop: asyncio.AbstractEventLoop
    # ADR-0003 §6/§7: per-connection clock offset, versioned so accepted
    # events can record which offset/version they were normalized against.
    clock_offset_ms: int = 0
    clock_offset_version: int = 1


@dataclass
class SessionSnapshot:
    """Server-authoritative session state (ADR-0001 §4, ADR-0004 §1).

    The snapshot served to clients is in-memory for M1; `models.py` persists
    only `session_state` (as `Session.status`) and `snapshot_revision`, which
    `_sync_lifecycle_to_db` keeps reconciled with the fields below. Playback
    and character state have no persisted counterpart yet, so a restart loses
    them (see implementation notes "Known Limitations").
    """

    snapshot_revision: int = 0
    session_state: str = "created"
    playback_state: str = "idle"
    active_clip: str | None = None
    character_state: str = "neutral"
    cooperation_level: int = 0
    revealed_facts: list[str] = field(default_factory=list)
    start_at_server_ms: int | None = None
    # ADR-0002 §5: consecutive fallbacks since the last ordinary clip. Served
    # in the snapshot so a reconnecting Wizard restores its warning state.
    consecutive_fallbacks: int = 0
    # Last clip `request_fallback` resolved to, so the next resolution in the
    # same category can avoid an immediate repeat (ADR-0002 §4). Server-internal
    # -- clients never need it, so it stays out of `to_message`.
    last_fallback_clip_id: str | None = None

    def bump(self) -> None:
        self.snapshot_revision += 1

    def resolve_scheduled_start(self) -> None:
        if (
            self.session_state == "starting"
            and self.start_at_server_ms is not None
            and _monotonic_ms() >= self.start_at_server_ms
        ):
            # The accepted scheduling event already bumped the snapshot. The
            # active state is derived from its fixed monotonic start time so
            # clients do not become stale against an unbroadcast revision.
            self.session_state = "active"
            self.start_at_server_ms = None

    def to_message(self) -> dict:
        self.resolve_scheduled_start()
        message = {
            "type": "session_snapshot",
            "snapshot_revision": self.snapshot_revision,
            "session_state": self.session_state,
            "playback_state": self.playback_state,
            "active_clip": self.active_clip,
            "character_state": self.character_state,
            "cooperation_level": self.cooperation_level,
            "revealed_facts": list(self.revealed_facts),
            "consecutive_fallbacks": self.consecutive_fallbacks,
            "server_timestamp_ms": _now_ms(),
        }
        if self.start_at_server_ms is not None:
            message["start_at_server_ms"] = self.start_at_server_ms
        return message


class ConnectionManager:
    """Tracks live WebSocket connections, session snapshots, and event dedup.

    Reconnect + missed-sequence backfill is snapshot-first (ADR-0001 §4): the
    client applies `session_snapshot` and then resends any not-yet-ACKed
    events, which flow back through the normal de-duplicated event path.
    """

    def __init__(self) -> None:
        # session_id -> role -> [ConnectionInfo, ...]
        # (single-connection roles hold at most one entry)
        self._connections: dict[str, dict[str, list[ConnectionInfo]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._snapshots: dict[str, SessionSnapshot] = defaultdict(SessionSnapshot)
        # session_id -> set of dedup keys for already-processed (accepted) events
        self._processed_event_ids: dict[str, set[str]] = defaultdict(set)
        # session_id -> case_id, memoised from the row read during the upgrade
        # so fallback resolution needs no extra query per command.
        self._case_ids: dict[str, str] = {}
        # session_id -> role -> wall-clock ms at which a single-connection role
        # lost its last connection (ADR-0002 §8).
        self._disconnected_since: dict[str, dict[str, int]] = defaultdict(dict)
        self._background_tasks: set[asyncio.Task[None]] = set()

    def snapshot_for(self, session_id: str) -> SessionSnapshot:
        return self._snapshots[session_id]

    def remember_case_id(self, session_id: str, case_id: str) -> None:
        self._case_ids[session_id] = case_id

    def case_id_for(self, session_id: str) -> str | None:
        return self._case_ids.get(session_id)

    async def connect(self, session_id: str, role: str, ws: WebSocket) -> bool:
        """Accept `ws` for (session_id, role).

        Returns True if this connection replaced a stale prior connection for
        a single-connection role (caller should notify other roles via
        `role_connection_replaced`). Raises RoleConnectionConflict if a
        single-connection role already has a live connection -- the caller
        must reject the new socket without accepting it.
        """
        replaced = False
        if role in SINGLE_CONNECTION_ROLES:
            existing = self._connections[session_id][role]
            if existing:
                prior = existing[0]
                if _now_ms() - prior.last_heartbeat_ms <= HEARTBEAT_STALE_MS:
                    raise RoleConnectionConflict(role)
                # Prior connection is stale (missed heartbeats past the
                # threshold): best-effort close it and take over.
                try:
                    await prior.websocket.close(
                        code=4409, reason="role_connection_replaced"
                    )
                except Exception:
                    pass
                existing.clear()
                replaced = True

        await ws.accept()
        now = _now_ms()
        self._connections[session_id][role].append(
            ConnectionInfo(
                websocket=ws,
                connected_at_ms=now,
                last_heartbeat_ms=now,
                event_loop=asyncio.get_running_loop(),
            )
        )
        # The role is back: clear any outstanding disconnect mark last, so a
        # takeover whose displaced socket reports its own disconnect while
        # `ws.accept()` is in flight cannot leave a stale mark behind.
        self._disconnected_since[session_id].pop(role, None)
        return replaced

    def disconnect(self, session_id: str, role: str, ws: WebSocket) -> None:
        conns = self._connections.get(session_id, {}).get(role, [])
        remaining = [c for c in conns if c.websocket is not ws]
        self._connections[session_id][role] = remaining
        if role in SINGLE_CONNECTION_ROLES and not remaining:
            # ADR-0002 §8 starts its clock here rather than pausing outright:
            # a reconnect inside DISCONNECT_GRACE_MS is smoothed over.
            self._disconnected_since[session_id][role] = _now_ms()

    def lost_critical_roles(self, session_id: str) -> list[str]:
        """Single-connection roles that count as lost right now (ADR-0002 §8).

        A role is lost when it either has no connection left and has been gone
        longer than `DISCONNECT_GRACE_MS`, or still holds a socket that has
        missed heartbeats for longer than `HEARTBEAT_STALE_MS` (ADR-0001 §2) --
        a half-open TCP connection looks alive but carries nothing.

        A role that never connected at all is *not* lost: a session where only
        the Wizard has joined yet is simply not underway.
        """
        now = _now_ms()
        lost: list[str] = []
        for role in sorted(SINGLE_CONNECTION_ROLES):
            conns = self._connections.get(session_id, {}).get(role, [])
            if conns:
                if all(now - c.last_heartbeat_ms > HEARTBEAT_STALE_MS for c in conns):
                    lost.append(role)
                continue
            since = self._disconnected_since.get(session_id, {}).get(role)
            if since is not None and now - since >= DISCONNECT_GRACE_MS:
                lost.append(role)
        return lost

    def touch_heartbeat(self, session_id: str, role: str, ws: WebSocket) -> None:
        for c in self._connections.get(session_id, {}).get(role, []):
            if c.websocket is ws:
                c.last_heartbeat_ms = _now_ms()
                return

    def first_live_connection(self, session_id: str) -> ConnectionInfo | None:
        for conns in self._connections.get(session_id, {}).values():
            if conns:
                return conns[0]
        return None

    def clock_offset_for(self, session_id: str, role: str, ws: WebSocket) -> tuple[int, int]:
        """Return (clock_offset_ms, clock_offset_version) for this connection."""
        for c in self._connections.get(session_id, {}).get(role, []):
            if c.websocket is ws:
                return c.clock_offset_ms, c.clock_offset_version
        return 0, 1

    def record_clock_offset(
        self, session_id: str, role: str, ws: WebSocket, offset_ms: int
    ) -> int:
        """Apply a client-reported offset (ADR-0003 §6) and return the new version."""
        for c in self._connections.get(session_id, {}).get(role, []):
            if c.websocket is ws:
                c.clock_offset_ms = offset_ms
                c.clock_offset_version += 1
                return c.clock_offset_version
        return 1

    def is_duplicate(self, session_id: str, dedup_key: str) -> bool:
        return dedup_key in self._processed_event_ids[session_id]

    def mark_processed(self, session_id: str, dedup_key: str) -> None:
        self._processed_event_ids[session_id].add(dedup_key)

    async def send_to(self, session_id: str, role: str, message: dict) -> None:
        for c in list(self._connections.get(session_id, {}).get(role, [])):
            await c.websocket.send_json(message)

    async def broadcast(
        self, session_id: str, message: dict, *, exclude: str | None = None
    ) -> None:
        for role, conns in self._connections.get(session_id, {}).items():
            if role == exclude:
                continue
            for c in list(conns):
                await c.websocket.send_json(message)

    def spawn_background_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


manager = ConnectionManager()


def _token_secret() -> bytes:
    secret = os.environ.get(WS_TOKEN_SECRET_ENV)
    if not secret:
        raise WebSocketAuthError("websocket_token_secret_not_configured")
    return secret.encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(f"{raw}{padding}".encode("ascii"))


def create_session_token(
    session_id: str,
    role: str,
    *,
    client_id: str = "temporary-client-id",
    expires_at_s: int | None = None,
) -> str:
    """Create a short-lived, role-bound token for the WS cookie.

    The join-code exchange endpoint that will issue this token belongs in a
    later Integration Agent task. Keeping the signer here lets the WebSocket
    upgrade validate a real HMAC-protected cookie now without trusting role or
    session claims from the URL alone.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role}")

    now_s = int(time.time())
    claims = {
        "sub": client_id,
        "session_id": session_id,
        "role": role,
        "iat": now_s,
        "exp": expires_at_s if expires_at_s is not None else now_s + WS_TOKEN_TTL_SECONDS,
    }
    payload = _b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(_token_secret(), payload.encode("ascii"), hashlib.sha256)
    return f"{payload}.{_b64encode(signature.digest())}"


def _cookie_value(websocket: WebSocket, name: str) -> str | None:
    cookie_header = websocket.headers.get("cookie")
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else None


def _verify_session_token(websocket: WebSocket, session_id: str, role: str) -> None:
    token = _cookie_value(websocket, WS_SESSION_COOKIE)
    if not token:
        raise WebSocketAuthError("missing_session_token")

    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError as exc:
        raise WebSocketAuthError("malformed_session_token") from exc

    expected_signature = hmac.new(
        _token_secret(), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        supplied_signature = _b64decode(signature_b64)
    except Exception as exc:
        raise WebSocketAuthError("malformed_session_token") from exc

    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise WebSocketAuthError("invalid_session_token")

    try:
        claims = json.loads(_b64decode(payload_b64))
    except Exception as exc:
        raise WebSocketAuthError("malformed_session_token") from exc

    try:
        expires_at_s = int(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WebSocketAuthError("malformed_session_token") from exc

    if expires_at_s < int(time.time()):
        raise WebSocketAuthError("expired_session_token")
    if claims.get("session_id") != session_id:
        raise WebSocketAuthError("session_token_mismatch")
    if claims.get("role") != role:
        raise WebSocketAuthError("role_token_mismatch")


def _event_dedup_key(envelope: EventEnvelope) -> str:
    """Idempotency key for event-ack de-duplication (ADR-0001 §7).

    The envelope schema (`events.py` / `packages/shared/src/events.ts`) has
    no dedicated `event_id` field, so per ADR-0001 §7's own fallback
    constraint (`UNIQUE(session_id, source, sequence)`), we key on
    `payload.event_id` or `payload.command_id` when the sender provides one,
    falling back to `session_id:source:sequence`.
    """
    explicit = envelope.payload.get("event_id") or envelope.payload.get("command_id")
    if explicit:
        return str(explicit)
    return f"{envelope.session_id}:{envelope.source.value}:{envelope.sequence}"


def _validate_short_lived_command(
    payload: dict, snapshot: SessionSnapshot
) -> str | None:
    """Return a rejection reason, or None if the command is still valid.

    ADR-0001 §5 / ADR-0004 §1: discard commands issued against a superseded
    snapshot revision, or received after their `expires_at_ms` deadline.
    """
    command_id = payload.get("command_id")
    expires_at_ms = payload.get("expires_at_ms")
    snapshot_revision = payload.get("snapshot_revision")
    if not command_id or expires_at_ms is None or snapshot_revision is None:
        return "missing_command_fields"
    if not isinstance(command_id, str) or not command_id.strip():
        return "invalid_command_fields"
    if isinstance(expires_at_ms, bool) or not isinstance(expires_at_ms, int):
        return "invalid_command_fields"
    if isinstance(snapshot_revision, bool) or not isinstance(snapshot_revision, int):
        return "invalid_command_fields"
    if snapshot_revision < 0:
        return "invalid_command_fields"
    if _monotonic_ms() > expires_at_ms:
        return "command_expired"
    if snapshot_revision < snapshot.snapshot_revision:
        return "stale_snapshot_revision"
    return None


async def _send_rejected_ack(
    websocket: WebSocket, event_id: str | None, reason: str
) -> None:
    await websocket.send_json(
        {
            "type": "event_ack",
            "event_id": event_id,
            "status": "rejected",
            "reason": reason,
        }
    )


def _validate_session_pause(payload: dict) -> str | None:
    reason = payload.get("reason")
    if reason is None:
        return "missing_pause_reason"
    if reason not in PAUSE_REASONS:
        return "invalid_pause_reason"
    return None


def _apply_snapshot_update(snapshot: SessionSnapshot, envelope: EventEnvelope) -> None:
    """Advance session_state/playback_state and bump snapshot_revision.

    Only called for accepted events; ADR-0004 §1 requires
    `snapshot_revision += 1` on every server-side state change.
    Character/cooperation/revealed_facts updates need a dedicated event type
    that does not yet exist in the shared contract -- see implementation
    notes "Known Limitations".

    Ordinary playback also clears the ADR-0002 §5 fallback streak: the counter
    measures *consecutive* fallbacks, so a real case answer in between ends the
    run. A fallback clip starting playback does not reset it -- that clip is the
    fallback the Wizard just requested.
    """
    et = envelope.event_type
    snapshot.resolve_scheduled_start()
    if et is EventType.FALLBACK_USED:
        # The streak counter itself is advanced by `_handle_request_fallback`,
        # which owns the ADR-0002 §5 accounting.
        snapshot.playback_state = "playing"
        snapshot.active_clip = envelope.payload.get("resolved_clip_id")
    elif et is EventType.SESSION_RESUMED:
        snapshot.session_state = "active"
        snapshot.start_at_server_ms = None
    elif et is EventType.SESSION_PAUSED:
        snapshot.session_state = "paused"
        snapshot.start_at_server_ms = None
    elif et is EventType.SESSION_ENDED:
        snapshot.session_state = "ended"
        snapshot.start_at_server_ms = None
    elif et is EventType.CLIP_COMMAND or et is EventType.CLIP_STARTED:
        snapshot.playback_state = "playing"
        clip_id = envelope.payload.get("clip_id")
        if clip_id is not None:
            snapshot.active_clip = clip_id
            case_id = manager.case_id_for(envelope.session_id)
            if case_id is not None and not cases.is_fallback_clip(case_id, str(clip_id)):
                snapshot.consecutive_fallbacks = 0
                snapshot.last_fallback_clip_id = None
    elif et in (EventType.CLIP_ENDED, EventType.CLIP_INTERRUPTED, EventType.RETURN_TO_IDLE):
        snapshot.playback_state = "idle"
        snapshot.active_clip = None
    else:
        return
    snapshot.bump()


def _schedule_session_start(snapshot: SessionSnapshot, start_at_server_ms: int) -> None:
    """Record ADR-0003's pending unified start without marking the session active."""
    snapshot.resolve_scheduled_start()
    snapshot.session_state = "starting"
    snapshot.start_at_server_ms = start_at_server_ms
    snapshot.bump()


# --- Persistence bridge (ADR-0004 §1-§4) -----------------------------------
#
# Everything below calls only `models.py`'s public access layer. `models.py`
# deliberately does not import this module (it would be a cycle), so the
# dependency runs one way: websocket -> models.

#: Accepted events that also move the persisted session row's state machine.
_DB_LIFECYCLE_EVENTS = {
    EventType.SESSION_STARTED,
    EventType.SESSION_PAUSED,
    EventType.SESSION_RESUMED,
    EventType.SESSION_ENDED,
}

#: Failures a persistence call may raise that must not tear the socket down.
#: `ValueError` covers `models._transition`'s illegal-transition refusal and
#: `pause_session`'s reason-enum check; `SessionNotFound` covers a session row
#: deleted while a socket was still open.
_PERSISTENCE_ERRORS = (ValueError, models.SessionNotFound, SQLAlchemyError)


@contextmanager
def _db_scope(websocket: WebSocket) -> Iterator[DBSession]:
    """Yield the DB session this connection's persistence calls should use.

    A long-lived socket is not a request, so there is no ``Depends(get_db)`` to
    resolve; the default is one short-lived ``models.session_scope()`` per unit
    of work, exactly as ``models.session_scope``'s docstring prescribes.

    When the app carries a ``get_db`` dependency override the override wins.
    That mirrors ``main._websocket_guard_db`` (main.py:84-107), which had to
    solve the same problem for the ASGI-level revocation guard: tests point
    ``get_db`` at a private in-memory database and open a real WebSocket
    against a session that exists *only* there, so honouring the override is
    what keeps the realtime layer writing to the same database as the REST
    surface it is being tested against.
    """
    app = websocket.scope.get("app")
    override = getattr(app, "dependency_overrides", {}).get(models.get_db)
    if override is None:
        with models.session_scope() as db:
            yield db
        return

    dependency = override()
    db = next(dependency)
    try:
        yield db
    finally:
        try:
            next(dependency)
        except StopIteration:
            pass


def _adopt_db_revision(snapshot: SessionSnapshot, db_revision: int) -> None:
    """Raise the in-memory revision to the persisted one when the row is ahead.

    The two counters do not advance in lockstep: clip playback bumps only the
    in-memory snapshot (there is no persisted playback state in M1), and the
    deferred ``starting -> active`` resolution bumps only the row. Reconciling
    by maximum keeps ``snapshot_revision`` monotonic in both directions of
    drift, which is what ADR-0004 §1 actually requires -- and monotonicity is
    load-bearing for `_validate_short_lived_command`'s staleness check.
    """
    if db_revision > snapshot.snapshot_revision:
        snapshot.snapshot_revision = db_revision


def _persist_envelope(
    websocket: WebSocket, session_id: str, envelope: EventEnvelope
) -> bool:
    """Store an accepted envelope; return True when a new row was written.

    Called before the ACK is sent: ADR-0004 §4 ranks raw events above every
    other artefact, so a client must never be told ``accepted`` for an event
    the database never saw.

    ``models.append_event`` is idempotent on both ADR-0001 §7 keys, so an event
    replayed after a server restart -- when the in-memory dedup set is empty
    but the row survives -- returns False here rather than duplicating a row.
    """
    with _db_scope(websocket) as db:
        try:
            _event, is_new = models.append_event(db, session_id, envelope)
        except Exception:
            db.rollback()
            raise
        return is_new


def _apply_db_lifecycle(
    db: DBSession,
    session_id: str,
    role: str,
    envelope: EventEnvelope,
    start_at_server_ms: int | None,
) -> models.Session:
    """Route one accepted lifecycle event to its `models.py` transition."""
    et = envelope.event_type
    if et is EventType.SESSION_STARTED:
        return models.start_session(db, session_id, start_at_ms=start_at_server_ms)
    if et is EventType.SESSION_PAUSED:
        # `_validate_session_pause` already pinned `reason` to the ADR-0004 §2
        # enum before the event was accepted.
        return models.pause_session(
            db, session_id, reason=envelope.payload["reason"], actor_role=role
        )
    if et is EventType.SESSION_RESUMED:
        return models.resume_session(db, session_id, actor_role=role)
    return models.end_session(db, session_id)


def _db_lifecycle_rejection_reason(
    websocket: WebSocket,
    session_id: str,
    envelope: EventEnvelope,
    start_at_server_ms: int | None,
) -> str | None:
    """Return a rejection reason when the persisted row cannot transition.

    This mirrors `models.py`'s transition contract before the raw event is
    appended, so rejected lifecycle commands do not leave an orphan event row.
    The actual mutation still goes through the public lifecycle functions.
    """
    if envelope.event_type not in _DB_LIFECYCLE_EVENTS:
        return None

    with _db_scope(websocket) as db:
        session = models.get_session(db, session_id)

    if session is None:
        return "session_not_found"

    current = session.status
    et = envelope.event_type
    if et is EventType.SESSION_RESUMED:
        return None if current == "paused" else "invalid_lifecycle_transition"
    if et is EventType.SESSION_ENDED and current == "ended":
        return None

    if et is EventType.SESSION_STARTED:
        target = "starting" if start_at_server_ms is not None else "active"
        if current == "paused":
            return "invalid_lifecycle_transition"
    elif et is EventType.SESSION_PAUSED:
        target = "paused"
    else:
        target = "ended"

    allowed = models.ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        return "invalid_lifecycle_transition"
    return None


def _sync_lifecycle_to_db(
    websocket: WebSocket,
    session_id: str,
    role: str,
    envelope: EventEnvelope,
    start_at_server_ms: int | None,
) -> int | None:
    """Mirror a new lifecycle event onto the persisted session row.

    The caller has already preflighted deterministic transition failures, so
    any exception here is treated as a persistence failure and prevents an
    accepted ACK.
    """
    if envelope.event_type not in _DB_LIFECYCLE_EVENTS:
        return None
    with _db_scope(websocket) as db:
        session = _apply_db_lifecycle(db, session_id, role, envelope, start_at_server_ms)
        return session.snapshot_revision


def _resolve_scheduled_start(
    websocket: WebSocket, session_id: str, snapshot: SessionSnapshot
) -> None:
    """Resolve a due scheduled start in memory and mirror it onto the row.

    ADR-0003 §5 fixes the unified start at a monotonic instant, so the flip to
    ``active`` is *derived* rather than broadcast. ``models.start_session`` is
    called a second time here -- the two-call sequence its own docstring
    describes -- to move the row ``starting -> active``.

    The row's revision is deliberately not adopted: the scheduling event
    already bumped the in-memory revision, and bumping again for a transition
    no client was ever told about would make every in-flight command look
    stale (the exact failure `SessionSnapshot.resolve_scheduled_start` avoids).
    """
    if snapshot.session_state != "starting":
        return
    snapshot.resolve_scheduled_start()
    if snapshot.session_state != "active":
        return
    with _db_scope(websocket) as db:
        try:
            models.start_session(db, session_id)
        except _PERSISTENCE_ERRORS as exc:
            db.rollback()
            logger.warning(
                "session %s: could not mark the scheduled start active in the "
                "persisted session row: %s",
                session_id,
                exc,
            )


def _record_takeover_audit(websocket: WebSocket, session_id: str, role: str) -> None:
    """Audit a single-connection-role takeover (ADR-0004 §3).

    Best-effort: the replacing socket has already been accepted, so losing the
    audit row must not take it back down. The failure is logged loudly instead.
    """
    with _db_scope(websocket) as db:
        try:
            models.record_audit(
                db,
                action=models.AUDIT_CONNECTION_TAKEOVER,
                actor_role=role,
                session_id=session_id,
                details={"replaced_role": role, "server_timestamp_ms": _now_ms()},
            )
        except _PERSISTENCE_ERRORS:
            db.rollback()
            logger.exception(
                "session %s: failed to audit the %s connection takeover",
                session_id,
                role,
            )


def _stamp_clock_offset(
    websocket: WebSocket, session_id: str, role: str, envelope: EventEnvelope
) -> None:
    """Stamp an envelope with this connection's clock offset (ADR-0003 §7).

    Applied once, at accept time: past events are never revisited when the
    offset later changes, so the stamped version records which correction the
    event was normalized against.
    """
    offset_ms, offset_version = manager.clock_offset_for(session_id, role, websocket)
    if envelope.clock_offset_ms is None:
        envelope.clock_offset_ms = offset_ms
    envelope.payload["clock_offset_version"] = offset_version
    envelope.payload["normalized_server_timestamp_ms"] = (
        envelope.client_timestamp_ms + envelope.clock_offset_ms
    )


# --- Fallback resolution (ADR-0002 §1-§5, §7) -------------------------------


async def _handle_request_fallback(
    websocket: WebSocket, session_id: str, role: str, raw: dict
) -> None:
    """Resolve a Wizard fallback request into an approved clip.

    ADR-0002 §1's hybrid model splits the decision: the Wizard sends a semantic
    *category*, and the server resolves which approved clip expresses it for the
    current case state. That is why this is a control message rather than a
    client-authored `fallback_used` envelope -- the client does not know the
    answer it is asking for. The resolution is then recorded as a normal
    `fallback_used` event (ADR-0002 §7) and broadcast to *every* role, including
    the requesting Wizard, which learns `resolved_clip_id` from it.

    The message shape (Wizard UI contract)::

        {"type": "request_fallback",          # or "command_type"
         "sequence": 12, "client_timestamp_ms": 45000,
         "payload": {"command_id": "cmd-f1", "expires_at_ms": ...,
                     "snapshot_revision": 3, "fallback_category": "CLARIFY",
                     "reason": "ambiguous_question"}}
    """
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        await _send_rejected_ack(websocket, None, "missing_command_fields")
        return

    command_id = payload.get("command_id")
    ack_id = str(command_id) if isinstance(command_id, str) and command_id else None

    if role != Source.WIZARD.value:
        await _send_rejected_ack(websocket, ack_id, "fallback_requires_wizard")
        return

    if ack_id is not None and manager.is_duplicate(session_id, ack_id):
        # De-duplication comes before every other check, as it does in the
        # generic event path: a retry after a missed ACK still carries the
        # snapshot revision from before the original was applied, and it must
        # not resolve a second clip or advance the ADR-0002 §5 streak.
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": ack_id,
                "status": "duplicate",
                "reason": None,
            }
        )
        return

    snapshot = manager.snapshot_for(session_id)
    _resolve_scheduled_start(websocket, session_id, snapshot)

    if snapshot.session_state == "paused":
        # ADR-0002 §8: while the session is technically paused the Wizard may
        # not push new answers at the student.
        await _send_rejected_ack(websocket, ack_id, "session_paused")
        return

    rejection_reason = _validate_short_lived_command(payload, snapshot)
    if rejection_reason is not None:
        await _send_rejected_ack(websocket, ack_id, rejection_reason)
        return

    category = payload.get("fallback_category")
    if category not in cases.FALLBACK_CATEGORIES:
        await _send_rejected_ack(websocket, ack_id, "invalid_fallback_category")
        return

    # `_validate_short_lived_command` has pinned command_id to a non-empty str.
    dedup_key = str(command_id)

    # The envelope fields are checked here rather than left to
    # `EventEnvelope.model_validate` below: a ValidationError escaping this
    # handler is answered by the receive loop with `event_id: None`, which the
    # Wizard cannot correlate back to the `command_id` it is waiting on.
    sequence = raw.get("sequence")
    client_timestamp_ms = raw.get("client_timestamp_ms")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or isinstance(client_timestamp_ms, bool)
        or not isinstance(client_timestamp_ms, int)
    ):
        await _send_rejected_ack(websocket, dedup_key, "invalid_envelope_fields")
        return

    case_id = manager.case_id_for(session_id)
    if case_id is None:
        await _send_rejected_ack(websocket, dedup_key, "unknown_case")
        return

    resolved_clip_id = cases.resolve_fallback_clip(
        case_id,
        category,
        snapshot.character_state,
        snapshot.revealed_facts,
        snapshot.last_fallback_clip_id,
    )
    consecutive_fallbacks = snapshot.consecutive_fallbacks + 1

    fallback_payload = dict(payload)
    fallback_payload.update(
        {
            "fallback_category": category,
            "resolved_clip_id": resolved_clip_id,
            "reason": payload.get("reason"),
            "wizard_selected": True,
            "consecutive_fallbacks": consecutive_fallbacks,
        }
    )
    envelope = EventEnvelope.model_validate(
        {
            "session_id": session_id,
            "sequence": sequence,
            "client_timestamp_ms": client_timestamp_ms,
            "source": role,
            "event_type": EventType.FALLBACK_USED.value,
            "payload": fallback_payload,
        }
    )
    envelope.server_timestamp_ms = _now_ms()
    _stamp_clock_offset(websocket, session_id, role, envelope)

    # ADR-0004 §4: durable before the ACK, exactly like the generic event path.
    try:
        is_new = _persist_envelope(websocket, session_id, envelope)
    except _PERSISTENCE_ERRORS:
        logger.exception(
            "session %s: failed to persist a fallback_used event from %s",
            session_id,
            role,
        )
        await _send_rejected_ack(websocket, dedup_key, "persistence_unavailable")
        return

    manager.mark_processed(session_id, dedup_key)
    if not is_new:
        # Already durable from an earlier process (in-memory dedup set lost to a
        # restart, row still there): re-ACK without advancing the streak.
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": dedup_key,
                "status": "duplicate",
                "reason": None,
            }
        )
        return

    snapshot.consecutive_fallbacks = consecutive_fallbacks
    snapshot.last_fallback_clip_id = resolved_clip_id
    _apply_snapshot_update(snapshot, envelope)

    await websocket.send_json(
        {
            "type": "event_ack",
            "event_id": dedup_key,
            "status": "accepted",
            "reason": None,
        }
    )
    await manager.broadcast(session_id, envelope.model_dump(mode="json"))

    if consecutive_fallbacks >= FALLBACK_THRESHOLD:
        # ADR-0002 §5. Sent as a control message: the ADR spells it with an
        # `event_type` key, but there is no `EventType` member for it and
        # `events.py`/`packages/shared` are a locked contract (see the
        # implementation notes).
        await manager.broadcast(
            session_id,
            {
                "type": "fallback_threshold_reached",
                "session_id": session_id,
                "payload": {"consecutive_fallbacks": consecutive_fallbacks},
                "server_timestamp_ms": _now_ms(),
            },
        )


# --- Disconnect staging / technical pause (ADR-0002 §8) ---------------------


def _next_server_sequence(websocket: WebSocket, session_id: str) -> int:
    """Next unused sequence number for a ``source == "server"`` event.

    Server-authored events belong to no client's sequence counter, and
    ``UNIQUE(session_id, source, sequence)`` (ADR-0001 §7) turns a reused one
    into a silent duplicate. Derived from the stored rows rather than a process
    counter so it stays correct across a restart; an automatic pause is rare
    enough that the extra read never touches the hot path.
    """
    with _db_scope(websocket) as db:
        events = models.load_events(db, session_id)
    return (
        max((e.sequence for e in events if e.source == Source.SERVER.value), default=-1)
        + 1
    )


def _pause_for_network_failure(
    websocket: WebSocket, session_id: str, lost_roles: list[str]
) -> EventEnvelope | None:
    """Pause a session whose critical role is gone (ADR-0002 §8, ADR-0004 §2).

    Returns the server-authored `session_paused` envelope for the caller to
    broadcast, or None when the persisted row was not in a pausable state.
    `models.pause_session` writes both the row and the ADR-0004 §3 audit entry.
    """
    with _db_scope(websocket) as db:
        session = models.get_session(db, session_id)
        if session is None or session.status != "active":
            return None
        try:
            session = models.pause_session(
                db,
                session_id,
                reason="network_failure",
                actor_role=Source.SERVER.value,
            )
        except _PERSISTENCE_ERRORS:
            db.rollback()
            logger.exception(
                "session %s: failed to pause after losing %s",
                session_id,
                ", ".join(lost_roles),
            )
            return None
        db_revision = session.snapshot_revision

    envelope = EventEnvelope(
        session_id=session_id,
        sequence=_next_server_sequence(websocket, session_id),
        # No client clock stands behind a server-authored event, so the
        # client-relative field is 0 and the wall-clock time is the server's.
        client_timestamp_ms=0,
        server_timestamp_ms=_now_ms(),
        clock_offset_ms=0,
        source=Source.SERVER,
        event_type=EventType.SESSION_PAUSED,
        payload={
            "event_id": f"server-network-pause:{session_id}:{db_revision}",
            "reason": "network_failure",
            "lost_roles": list(lost_roles),
            "auto_paused": True,
        },
    )
    try:
        _persist_envelope(websocket, session_id, envelope)
    except _PERSISTENCE_ERRORS:
        # The pause is already durable on the session row and in the audit log;
        # losing its timeline event must not undo it.
        logger.exception(
            "session %s: failed to persist the automatic network-failure pause",
            session_id,
        )

    snapshot = manager.snapshot_for(session_id)
    snapshot.session_state = "paused"
    snapshot.start_at_server_ms = None
    snapshot.bump()
    _adopt_db_revision(snapshot, db_revision)
    manager.mark_processed(session_id, str(envelope.payload["event_id"]))
    return envelope


async def _evaluate_lost_critical_roles(websocket: WebSocket, session_id: str) -> None:
    """Pause the session if student or wizard has dropped out (ADR-0002 §8).

    The in-memory state guard runs first so a session that is not underway
    (including one that is already paused) costs two dict lookups and no query.
    """
    snapshot = manager.snapshot_for(session_id)
    if snapshot.session_state not in ("starting", "active"):
        return
    lost_roles = manager.lost_critical_roles(session_id)
    if not lost_roles:
        return

    _resolve_scheduled_start(websocket, session_id, snapshot)
    if snapshot.session_state != "active":
        return

    envelope = _pause_for_network_failure(websocket, session_id, lost_roles)
    if envelope is None:
        return
    await manager.broadcast(session_id, envelope.model_dump(mode="json"))


async def _delayed_disconnect_pause_check(
    websocket: WebSocket, session_id: str, role: str
) -> None:
    """Re-check a critical-role disconnect after ADR-0002's grace window."""
    try:
        await asyncio.sleep(DISCONNECT_GRACE_MS / 1000)
        await _evaluate_lost_critical_roles(websocket, session_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "session %s: delayed disconnect check for %s failed", session_id, role
        )


async def _handle_message(
    websocket: WebSocket, session_id: str, role: str, raw: object
) -> None:
    """Route one incoming JSON message to the correct handler.

    Control-plane messages (`ping`, `reconnect_hello`) carry a top-level
    `type` field and never overlap with the `EventEnvelope` shape, which is
    keyed on `event_type` instead -- so dispatch on `type` first and fall
    back to envelope parsing.
    """
    if not isinstance(raw, dict):
        await _send_rejected_ack(websocket, None, "invalid_message")
        return

    # Any inbound frame proves this socket is alive, not just a `ping`.
    # ADR-0001 §2's threshold measures *silence*, so a Wizard working steadily
    # through clip commands whose pings happen to lapse must not be judged lost
    # -- otherwise the check below would pause the session on the strength of
    # that Wizard's own message.
    manager.touch_heartbeat(session_id, role, websocket)

    # Inbound traffic also catches heartbeat-stale critical roles that still
    # hold an apparently open socket; closed sockets get their own delayed
    # grace-window check from the disconnect handler.
    await _evaluate_lost_critical_roles(websocket, session_id)

    # ADR-0002 §2 writes the fallback request with a `command_type` key; the
    # rest of the control plane uses `type`. Accept either spelling.
    msg_type = raw.get("type") or raw.get("command_type")

    if msg_type == "request_fallback":
        await _handle_request_fallback(websocket, session_id, role, raw)
        return

    if msg_type == "ping":
        # The heartbeat itself was already recorded above, for every message.
        await websocket.send_json(
            {
                "type": "pong",
                "ping_id": raw.get("ping_id"),
                "server_timestamp_ms": _now_ms(),
            }
        )
        return

    if msg_type == "clock_sync_probe":
        # ADR-0003 §2/§3: record receive time s1 immediately, then take s2
        # right before sending so the client can compute RTT/offset.
        s1 = _monotonic_ms()
        s2 = _monotonic_ms()
        await websocket.send_json(
            {
                "type": "clock_sync_response",
                "probe_id": raw.get("probe_id"),
                "t0": raw.get("t0"),
                "s1": s1,
                "s2": s2,
            }
        )
        return

    if msg_type == "clock_offset_report":
        # ADR-0003 §6: client has already filtered/median'd its own samples
        # (see compute_offset_median); the server just records the result.
        # Malformed reports are dropped without a reply -- offset reporting
        # is best-effort and never blocks the connection.
        offset_ms = raw.get("offset_ms")
        sample_count = raw.get("sample_count")
        rtt_ms = raw.get("rtt_ms")
        valid = all(
            isinstance(v, int) and not isinstance(v, bool)
            for v in (offset_ms, sample_count, rtt_ms)
        )
        if valid:
            manager.record_clock_offset(session_id, role, websocket, offset_ms)
        return

    if msg_type == "reconnect_hello":
        # Snapshot-first (ADR-0001 §4): reply with the authoritative snapshot
        # before the client resends any not-yet-ACKed events. The client's
        # `pending_event_ids` resend as ordinary envelope events afterwards
        # and flow through the normal de-duplicated accept/duplicate path
        # below, so no separate replay bookkeeping is needed here.
        manager.touch_heartbeat(session_id, role, websocket)
        snapshot = manager.snapshot_for(session_id)
        _resolve_scheduled_start(websocket, session_id, snapshot)
        await websocket.send_json(snapshot.to_message())
        return

    # Anything else is expected to be a standard event envelope. The URL path
    # role/session have already been bound to the verified cookie; never trust
    # client-supplied identity fields in the envelope.
    payload = raw.get("payload", {})
    event_id = payload.get("event_id") if isinstance(payload, dict) else None
    if event_id is not None:
        event_id = str(event_id)
    if raw.get("session_id") not in (None, session_id):
        await _send_rejected_ack(websocket, event_id, "session_id_mismatch")
        return
    if raw.get("source") not in (None, role):
        await _send_rejected_ack(websocket, event_id, "source_role_mismatch")
        return
    if role not in EVENT_SOURCE_ROLES:
        await _send_rejected_ack(websocket, event_id, "read_only_role_cannot_emit_events")
        return

    raw["session_id"] = session_id
    raw["source"] = role
    envelope = EventEnvelope.model_validate(raw)
    envelope.server_timestamp_ms = _now_ms()
    _stamp_clock_offset(websocket, session_id, role, envelope)

    dedup_key = _event_dedup_key(envelope)
    snapshot = manager.snapshot_for(session_id)
    # A start scheduled earlier may have come due while the socket was idle;
    # settle that before validating anything against the snapshot.
    _resolve_scheduled_start(websocket, session_id, snapshot)

    if manager.is_duplicate(session_id, dedup_key):
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": dedup_key,
                "status": "duplicate",
                "reason": None,
            }
        )
        return

    if envelope.event_type in SHORT_LIVED_COMMAND_TYPES:
        rejection_reason = _validate_short_lived_command(envelope.payload, snapshot)
        if rejection_reason is not None:
            await websocket.send_json(
                {
                    "type": "event_ack",
                    "event_id": dedup_key,
                    "status": "rejected",
                    "reason": rejection_reason,
                }
            )
            return

    if envelope.event_type is EventType.SESSION_PAUSED:
        rejection_reason = _validate_session_pause(envelope.payload)
        if rejection_reason is not None:
            await websocket.send_json(
                {
                    "type": "event_ack",
                    "event_id": dedup_key,
                    "status": "rejected",
                    "reason": rejection_reason,
                }
            )
            return

    if envelope.event_type is EventType.FALLBACK_USED:
        # ADR-0002 §1: which approved clip expresses a category is the server's
        # call. A client-authored `fallback_used` would route around that (and
        # around the §5 streak accounting), so the Wizard must go through the
        # `request_fallback` control message instead.
        await _send_rejected_ack(websocket, dedup_key, "fallback_is_server_resolved")
        return

    if envelope.event_type is EventType.UNCOVERED_QUESTION and role != Source.WIZARD.value:
        # spec §6/§8: marking an uncovered question is the Wizard's `M` hotkey.
        await _send_rejected_ack(websocket, dedup_key, "uncovered_question_requires_wizard")
        return

    start_at_server_ms: int | None = None
    if envelope.event_type is EventType.SESSION_STARTED:
        start_at_server_ms = _monotonic_ms() + 2000

    try:
        lifecycle_rejection_reason = _db_lifecycle_rejection_reason(
            websocket, session_id, envelope, start_at_server_ms
        )
    except _PERSISTENCE_ERRORS:
        logger.exception(
            "session %s: failed to validate a %s lifecycle transition from %s",
            session_id,
            envelope.event_type.value,
            role,
        )
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": dedup_key,
                "status": "rejected",
                "reason": "persistence_unavailable",
            }
        )
        return
    if lifecycle_rejection_reason is not None:
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": dedup_key,
                "status": "rejected",
                "reason": lifecycle_rejection_reason,
            }
        )
        return

    # ADR-0004 §4: the raw event is durable before the client is told
    # `accepted`. On failure nothing is marked processed, so a retry of the
    # same event can still succeed.
    try:
        is_new = _persist_envelope(websocket, session_id, envelope)
    except _PERSISTENCE_ERRORS:
        logger.exception(
            "session %s: failed to persist a %s event from %s",
            session_id,
            envelope.event_type.value,
            role,
        )
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": dedup_key,
                "status": "rejected",
                "reason": "persistence_unavailable",
            }
        )
        return

    if not is_new:
        # Already durable from an earlier process: the in-memory dedup set does
        # not survive a restart, but the row does. Re-ACK as a duplicate rather
        # than re-applying the snapshot update or re-broadcasting it.
        manager.mark_processed(session_id, dedup_key)
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": dedup_key,
                "status": "duplicate",
                "reason": None,
            }
        )
        return

    db_revision: int | None = None
    try:
        db_revision = _sync_lifecycle_to_db(
            websocket, session_id, role, envelope, start_at_server_ms
        )
    except _PERSISTENCE_ERRORS:
        logger.exception(
            "session %s: failed to apply a %s lifecycle transition from %s",
            session_id,
            envelope.event_type.value,
            role,
        )
        await websocket.send_json(
            {
                "type": "event_ack",
                "event_id": dedup_key,
                "status": "rejected",
                "reason": "persistence_unavailable",
            }
        )
        return

    if envelope.event_type is EventType.SESSION_STARTED:
        _schedule_session_start(snapshot, start_at_server_ms)
    else:
        _apply_snapshot_update(snapshot, envelope)

    if db_revision is not None:
        _adopt_db_revision(snapshot, db_revision)

    manager.mark_processed(session_id, dedup_key)
    await websocket.send_json(
        {
            "type": "event_ack",
            "event_id": dedup_key,
            "status": "accepted",
            "reason": None,
        }
    )

    if envelope.event_type is EventType.SESSION_STARTED:
        # ADR-0003 §5: don't flip to "active" the instant the request lands --
        # broadcast a future start time so all roles (and the recorder) begin
        # together. The snapshot records this as a pending start until the
        # scheduled monotonic time becomes effective.
        await manager.broadcast(
            session_id,
            {
                "type": "session_start_scheduled",
                "session_id": session_id,
                "start_at_server_ms": start_at_server_ms,
                "server_timestamp_ms": _now_ms(),
            },
        )

    message = envelope.model_dump(mode="json")
    if envelope.event_type is EventType.UNCOVERED_QUESTION:
        await manager.send_to(session_id, "teacher", message)
        await manager.send_to(session_id, "observer", message)
        return

    await manager.broadcast(session_id, message, exclude=role)


@router.websocket("/ws/{session_id}/{role}")
async def session_ws(websocket: WebSocket, session_id: str, role: str) -> None:
    """One socket per (session, role).

    role is one of 'student', 'wizard' (single active connection each) or
    'teacher', 'observer' (multiple read-only connections allowed) per
    ADR-0001 §9.

    The full join-code exchange endpoint belongs to a later Integration Agent
    task, but the upgrade itself already requires an HMAC-protected,
    role-bound cookie so the path role is never treated as authoritative by
    itself (ADR-0001 §8).
    """
    if role not in VALID_ROLES:
        await websocket.close(code=4400, reason="invalid_role")
        return
    try:
        _verify_session_token(websocket, session_id, role)
    except WebSocketAuthError as exc:
        await websocket.close(code=4401, reason=exc.reason)
        return

    # A valid token proves the bearer's role and session claim, not that the
    # session it names was ever created. Refuse before any in-memory state is
    # spun up for a session no event could be persisted against.
    try:
        with _db_scope(websocket) as db:
            session = models.get_session(db, session_id)
            session_exists = session is not None
            if session is not None:
                # Memoised here so ADR-0002 fallback resolution never needs a
                # query of its own on the realtime path.
                manager.remember_case_id(session_id, session.case_id)
    except SQLAlchemyError:
        logger.exception(
            "failed to look up session %s while upgrading a %s socket",
            session_id,
            role,
        )
        # Same close signal main.py's ASGI guard already emits when it cannot
        # read session state, so clients need no new code to handle this.
        await websocket.close(code=4401, reason="session_state_unavailable")
        return
    if not session_exists:
        await websocket.close(code=4404, reason="session_not_found")
        return

    try:
        replaced = await manager.connect(session_id, role, websocket)
    except RoleConnectionConflict:
        # ADR-0001 §9: a live connection must not be silently evicted by a
        # new one for the same role -- reject the new connection instead.
        await websocket.close(code=4409, reason="role_already_connected")
        return

    if replaced:
        # ADR-0004 §3: a takeover is security-relevant, so it is audited before
        # it is announced to the other roles (ADR-0001 §9).
        _record_takeover_audit(websocket, session_id, role)
        await manager.broadcast(
            session_id,
            {
                "type": "role_connection_replaced",
                "role": role,
                "server_timestamp_ms": _now_ms(),
            },
            exclude=role,
        )

    try:
        while True:
            try:
                raw = await websocket.receive_json()
                await _handle_message(websocket, session_id, role, raw)
            except ValidationError as exc:
                await websocket.send_json(
                    {
                        "type": "event_ack",
                        "event_id": None,
                        "status": "rejected",
                        "reason": f"invalid_envelope: {exc.error_count()} error(s)",
                    }
                )
            except ValueError:
                await _send_rejected_ack(websocket, None, "invalid_json")
    except WebSocketDisconnect:
        manager.disconnect(session_id, role, websocket)
        if role in SINGLE_CONNECTION_ROLES:
            live_connection = manager.first_live_connection(session_id)
            if live_connection is not None:
                live_connection.event_loop.call_soon_threadsafe(
                    manager.spawn_background_task,
                    _delayed_disconnect_pause_check(
                        live_connection.websocket, session_id, role
                    ),
                )
            else:
                manager.spawn_background_task(
                    _delayed_disconnect_pause_check(websocket, session_id, role)
                )
