"""SQLAlchemy models + SQLite engine (spec §5, §9, §10).

The Session Server owns all local storage. Sessions and their events are
persisted to SQLite; recording chunks/media live on the local filesystem
under ``sessions/`` and ``cases/`` (see ``recording.py`` / ``cases.py``).

On top of the schema this module exposes the **persistence access layer** the
rest of the server calls (ADR-0004 §1–§4):

  - session lifecycle: :func:`create_session`, :func:`get_session`,
    :func:`start_session`, :func:`pause_session`, :func:`resume_session`,
    :func:`end_session` -- a 5-state machine matching
    ``websocket.SessionSnapshot`` (``created`` -> ``starting`` -> ``active``
    -> ``paused`` -> ``ended``), bumping ``snapshot_revision`` on every
    state change (ADR-0004 §1) and validating ``pause_reason`` against the
    ADR-0004 §2 enum.
  - event persistence: :func:`append_event` (idempotent) and
    :func:`load_events` (ordered replay for the teacher timeline). Persisted
    events are immutable -- nothing here updates or deletes an ``events`` row
    (ADR-0004 §4 raw-event retention priority).
  - audit: :func:`record_audit`, which only ever stores a SHA-256 digest of a
    token, never the token itself (ADR-0004 §3).

``websocket.py`` is owned by another roadmap role and is deliberately *not*
imported here (it would become a cycle once the realtime layer starts calling
into persistence). The two constants that must stay in lockstep with it --
:data:`PAUSE_REASONS` and the :func:`event_dedup_key` algorithm -- are mirrored
locally and pinned by equivalence assertions in
``server/tests/test_persistence.py``.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.orm import Session as DBSession

from .events import EventEnvelope

DB_URL = os.environ.get("MINDPROBE_DB_URL", "sqlite:///./sessions/mind-probe.sqlite")

# check_same_thread=False so the async FastAPI workers can share the engine.
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- State machine / enums (ADR-0004 §1, §2) --------------------------------

#: The five session states, matching ``websocket.SessionSnapshot.session_state``.
SESSION_STATES = ("created", "starting", "active", "paused", "ended")

#: Legal state transitions. ``ended`` is terminal.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # A session may be started directly (``active``) or scheduled first
    # (``starting``, mirroring ADR-0003 §5 / ``websocket._schedule_session_start``).
    "created": frozenset({"starting", "active", "ended"}),
    "starting": frozenset({"active", "paused", "ended"}),
    "active": frozenset({"paused", "ended"}),
    # ``paused -> active`` is valid only through ``resume_session()``, which
    # clears ``pause_reason`` to preserve the persisted-state invariant.
    "paused": frozenset({"active", "ended"}),
    "ended": frozenset(),
}

#: ADR-0004 §2 pause reasons. Mirrors ``websocket.PAUSE_REASONS``; the two are
#: asserted equal in ``tests/test_persistence.py``.
PAUSE_REASONS = frozenset(
    {
        "manual",
        "network_failure",
        "clock_sync_failure",
        "recording_failure",
        "ethical_or_safety_stop",
    }
)

# Roles that receive one-time join codes when a session is created (ADR-0001
# §8). Observer is a valid WebSocket role, but not part of the default
# Student/Wizard/Teacher bootstrap set.
JOIN_CODE_ROLES = ("student", "wizard", "teacher")
JOIN_CODE_ALLOWED_ROLES = frozenset({*JOIN_CODE_ROLES, "observer"})

# --- Audit actions (ADR-0004 §3) --------------------------------------------
AUDIT_TOKEN_ISSUED = "token_issued"
AUDIT_TOKEN_REVOKED = "token_revoked"
AUDIT_SESSION_PAUSED = "session_paused"
AUDIT_CONNECTION_TAKEOVER = "connection_takeover"


class Base(DeclarativeBase):
    pass


class Session(Base):
    """A single interview session (spec §3, §9)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. "S001"
    case_id: Mapped[str] = mapped_column(String, index=True)
    # session-level access token (審閱後修訂 6)
    token: Mapped[str | None] = mapped_column(String, nullable=True)
    # One of SESSION_STATES; transitions go through the helpers below so
    # snapshot_revision stays in step (ADR-0004 §1).
    status: Mapped[str] = mapped_column(String, default="created")
    # Monotonic counter bumped on every server-side state change (ADR-0004 §1).
    snapshot_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set while status == "paused"; always one of PAUSE_REASONS (ADR-0004 §2).
    pause_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    start_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list["Event"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    join_codes: Mapped[list["JoinCode"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class JoinCode(Base):
    """One-time code used to redeem a role-bound WebSocket token.

    The raw code is returned once from ``issue_join_codes`` and is never stored.
    Only its SHA-256 digest is persisted, so losing the DB does not expose
    usable join credentials.
    """

    __tablename__ = "join_codes"
    __table_args__ = (
        UniqueConstraint("session_id", "role", name="uq_join_codes_session_role"),
        UniqueConstraint("code_hash", name="uq_join_codes_code_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String, index=True)
    code_hash: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    session: Mapped[Session] = relationship(back_populates="join_codes")


class Event(Base):
    """A persisted event envelope (spec §9).

    Rows are append-only: ADR-0004 §4 ranks raw events above every other
    artefact, so :func:`append_event` never updates or deletes an existing row.
    Both idempotency keys from ADR-0001 §7 are enforced in the database rather
    than only in application code, so concurrent writers cannot race a
    duplicate in:

      - ``UNIQUE(session_id, source, sequence)`` -- the ADR's own fallback
        constraint, quoted in ``websocket._event_dedup_key``;
      - ``UNIQUE(session_id, dedup_key)`` -- the sender-supplied
        ``payload.event_id`` / ``payload.command_id`` when present.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "source", "sequence", name="uq_events_session_source_sequence"
        ),
        UniqueConstraint("session_id", "dedup_key", name="uq_events_session_dedup_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), index=True
    )
    # Idempotency key computed by event_dedup_key(); mirrors
    # websocket._event_dedup_key() so the in-memory ACK path and the DB agree.
    dedup_key: Mapped[str] = mapped_column(String, index=True)
    sequence: Mapped[int] = mapped_column(Integer, index=True)
    client_timestamp_ms: Mapped[int] = mapped_column(Integer)
    server_timestamp_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clock_offset_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    session: Mapped[Session] = relationship(back_populates="events")


class RecordingChunk(Base):
    """Metadata for an uploaded MediaRecorder chunk (spec §10.1)."""

    __tablename__ = "recording_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String, nullable=True)
    upload_status: Mapped[str] = mapped_column(String, default="received")
    path: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """Security-relevant action log (ADR-0004 §3).

    Records who paused a session, who took a connection over, and when a token
    was issued or revoked. ``session_id`` is intentionally *not* a foreign key:
    audit rows must outlive the session row they describe.

    Raw tokens are never stored -- only ``token_hash``, a SHA-256 hex digest
    (see :func:`record_audit`).
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    action: Mapped[str] = mapped_column(String, index=True)
    actor_role: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


def init_db() -> None:
    """Create tables. Called on server startup (spec §5).

    ``create_all`` only creates *missing* tables -- it does not alter existing
    ones. A sqlite file created before ``snapshot_revision`` / ``pause_reason``
    / ``events.dedup_key`` / ``audit_logs`` existed must be deleted and
    recreated; there is no migration tool wired up yet.
    """
    os.makedirs("./sessions", exist_ok=True)
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Session handles
# ---------------------------------------------------------------------------


def get_db() -> Iterator[DBSession]:
    """FastAPI dependency yielding a request-scoped DB session.

    Use with ``Depends(get_db)`` from a route. Non-request callers (the
    WebSocket handler, background tasks) should use :func:`session_scope`.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[DBSession]:
    """Context-managed DB session for code outside the request lifecycle.

    ``websocket.py`` holds a long-lived socket rather than a request, so it
    should open one of these per unit of work instead of reusing a session for
    the whole connection.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Session lifecycle (ADR-0004 §1, §2)
# ---------------------------------------------------------------------------


class SessionNotFound(LookupError):
    """Raised when an operation targets a session id that is not persisted."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"unknown session: {session_id}")
        self.session_id = session_id


class JoinCodeInvalid(LookupError):
    """Raised when a submitted join code does not belong to the session."""


class JoinCodeConsumed(ValueError):
    """Raised when a one-time join code has already been redeemed."""


def _require_session(db: DBSession, session_id: str) -> Session:
    session = get_session(db, session_id)
    if session is None:
        raise SessionNotFound(session_id)
    return session


def _transition(session: Session, target: str) -> None:
    """Move `session` to `target` and bump snapshot_revision (ADR-0004 §1).

    Raises ValueError for a transition the state machine does not allow, so an
    out-of-order client can never drive the stored state somewhere the
    in-memory ``SessionSnapshot`` would not go.
    """
    if target not in SESSION_STATES:
        raise ValueError(f"invalid session state: {target}")
    allowed = ALLOWED_TRANSITIONS[session.status]
    if target not in allowed:
        raise ValueError(
            f"illegal session transition: {session.status} -> {target} "
            f"(allowed: {sorted(allowed) or 'none, terminal'})"
        )
    session.status = target
    session.snapshot_revision += 1


def create_session(db: DBSession, session_id: str, case_id: str) -> Session:
    """Create a session in the initial ``created`` state at revision 0."""
    session = Session(
        id=session_id,
        case_id=case_id,
        status="created",
        snapshot_revision=0,
    )
    db.add(session)
    db.commit()
    return session


def get_session(db: DBSession, session_id: str) -> Session | None:
    return db.get(Session, session_id)


def hash_join_code(code: str) -> str:
    """SHA-256 hex digest of a one-time join code."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def issue_join_codes(
    db: DBSession, session_id: str, roles: tuple[str, ...] = JOIN_CODE_ROLES
) -> dict[str, str]:
    """Issue one raw join code per role for a newly-created session.

    Raw codes are returned to the caller exactly once and never stored. The DB
    receives only ``code_hash`` rows; ``consume_join_code`` is the only path
    that resolves one back to a role.
    """
    _require_session(db, session_id)
    codes: dict[str, str] = {}
    for role in roles:
        if role not in JOIN_CODE_ALLOWED_ROLES:
            raise ValueError(f"invalid join-code role: {role}")
        raw_code = secrets.token_urlsafe(24)
        db.add(
            JoinCode(
                session_id=session_id,
                role=role,
                code_hash=hash_join_code(raw_code),
            )
        )
        codes[role] = raw_code
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"join codes already issued for session: {session_id}") from None
    return codes


def consume_join_code(
    db: DBSession, session_id: str, code: str, *, commit: bool = True
) -> JoinCode:
    """Consume a one-time join code and return its server-owned role binding.

    The row update is guarded by ``consumed_at IS NULL`` so two concurrent
    redeemers cannot both win. Pass ``commit=False`` when a caller needs code
    consumption to be part of a larger transaction, such as token issuance plus
    audit logging in ``POST /sessions/{id}/join``.
    """
    code_hash = hash_join_code(code)
    join_code = db.scalars(
        select(JoinCode)
        .where(JoinCode.session_id == session_id)
        .where(JoinCode.code_hash == code_hash)
        .limit(1)
    ).first()
    if join_code is None:
        raise JoinCodeInvalid("invalid join code")
    if join_code.consumed_at is not None:
        raise JoinCodeConsumed("join code already consumed")
    consumed_at = _utcnow()
    result = db.execute(
        update(JoinCode)
        .where(JoinCode.id == join_code.id)
        .where(JoinCode.consumed_at.is_(None))
        .values(consumed_at=consumed_at)
    )
    if result.rowcount != 1:
        db.rollback()
        raise JoinCodeConsumed("join code already consumed")
    join_code.consumed_at = consumed_at
    if commit:
        db.commit()
    else:
        db.flush()
    return join_code


def start_session(
    db: DBSession, session_id: str, start_at_ms: int | None = None
) -> Session:
    """Start a session.

    With `start_at_ms` the session enters ``starting`` and holds the unified
    start time (ADR-0003 §5); without it, it goes straight to ``active``. The
    realtime layer calls this twice -- once to schedule, once when the
    scheduled monotonic time is reached.
    """
    session = _require_session(db, session_id)
    if session.status == "paused":
        raise ValueError("cannot start a paused session; use resume_session()")
    _transition(session, "starting" if start_at_ms is not None else "active")
    if start_at_ms is not None:
        session.start_at_ms = start_at_ms
    db.commit()
    return session


def pause_session(
    db: DBSession, session_id: str, reason: str, actor_role: str = "wizard"
) -> Session:
    """Pause a session with an ADR-0004 §2 reason, and audit who did it.

    Raises ValueError for a reason outside the enum -- technical pauses are
    excluded from student scoring downstream, so an unclassified pause is not
    accepted.
    """
    if reason not in PAUSE_REASONS:
        raise ValueError(
            f"invalid pause_reason: {reason!r} (expected one of {sorted(PAUSE_REASONS)})"
        )
    session = _require_session(db, session_id)
    _transition(session, "paused")
    session.pause_reason = reason
    record_audit(
        db,
        action=AUDIT_SESSION_PAUSED,
        actor_role=actor_role,
        session_id=session_id,
        details={"reason": reason, "snapshot_revision": session.snapshot_revision},
        commit=False,
    )
    db.commit()
    return session


def resume_session(db: DBSession, session_id: str, actor_role: str = "wizard") -> Session:
    """Resume a paused session, clearing its pause reason."""
    session = _require_session(db, session_id)
    if session.status != "paused":
        raise ValueError(f"cannot resume session in state: {session.status}")
    _transition(session, "active")
    session.pause_reason = None
    db.commit()
    return session


def end_session(db: DBSession, session_id: str, *, commit: bool = True) -> Session:
    """End a session. Idempotent: ending an already-ended session is a no-op."""
    session = _require_session(db, session_id)
    if session.status == "ended":
        return session
    _transition(session, "ended")
    session.ended_at = _utcnow()
    if commit:
        db.commit()
    else:
        db.flush()
    return session


# ---------------------------------------------------------------------------
# Event persistence (ADR-0001 §7, ADR-0004 §4)
# ---------------------------------------------------------------------------


def event_dedup_key(envelope: EventEnvelope) -> str:
    """Idempotency key for a persisted event.

    Deliberately byte-for-byte equivalent to ``websocket._event_dedup_key()``
    -- including its ``or`` truthiness (an empty-string ``event_id`` falls
    through to ``command_id``, then to the triple). Duplicated rather than
    imported because ``websocket.py`` will start importing this module in the
    next roadmap slice, which would make a direct import circular. The
    equivalence is pinned by a test.
    """
    explicit = envelope.payload.get("event_id") or envelope.payload.get("command_id")
    if explicit:
        return str(explicit)
    return f"{envelope.session_id}:{envelope.source.value}:{envelope.sequence}"


def _find_existing_event(
    db: DBSession, session_id: str, source: str, sequence: int, dedup_key: str
) -> Event | None:
    """Look up an already-persisted event by either idempotency key."""
    return db.scalars(
        select(Event)
        .where(Event.session_id == session_id)
        .where(
            (Event.dedup_key == dedup_key)
            | ((Event.source == source) & (Event.sequence == sequence))
        )
        .order_by(Event.id)
        .limit(1)
    ).first()


def append_event(
    db: DBSession, session_id: str, envelope: EventEnvelope
) -> tuple[Event, bool]:
    """Idempotently persist one event envelope.

    Returns ``(event, is_new)``. On a duplicate -- same
    ``payload.event_id``/``command_id``, or same
    ``(session_id, source, sequence)`` -- the stored row is returned untouched
    and ``is_new`` is False; existing events are never rewritten (ADR-0004 §4).

    Takes the whole :class:`~app.events.EventEnvelope` rather than loose
    fields: the envelope is the contract (spec §9), and the dedup key is
    derived here so no caller can supply its own.
    """
    if envelope.session_id != session_id:
        raise ValueError(
            f"envelope session_id {envelope.session_id!r} does not match {session_id!r}"
        )
    _require_session(db, session_id)

    source = envelope.source.value
    dedup_key = event_dedup_key(envelope)

    existing = _find_existing_event(
        db, session_id, source, envelope.sequence, dedup_key
    )
    if existing is not None:
        return existing, False

    event = Event(
        session_id=session_id,
        dedup_key=dedup_key,
        sequence=envelope.sequence,
        client_timestamp_ms=envelope.client_timestamp_ms,
        server_timestamp_ms=envelope.server_timestamp_ms,
        clock_offset_ms=envelope.clock_offset_ms,
        source=source,
        event_type=envelope.event_type.value,
        payload=dict(envelope.payload),
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race against a concurrent writer holding the same key. The
        # winner's row stands; we report it as a duplicate.
        db.rollback()
        existing = _find_existing_event(
            db, session_id, source, envelope.sequence, dedup_key
        )
        if existing is None:
            raise
        return existing, False
    return event, True


def load_events(db: DBSession, session_id: str) -> list[Event]:
    """Return this session's events ordered by ``sequence`` for replay.

    This is the basis of the teacher-side unified timeline (ADR-0004 M1 pass
    criteria). ``id`` breaks ties so the order is deterministic even if a
    sequence number is ever reused across sources.
    """
    return list(
        db.scalars(
            select(Event)
            .where(Event.session_id == session_id)
            .order_by(Event.sequence, Event.id)
        )
    )


# ---------------------------------------------------------------------------
# Audit log (ADR-0004 §3)
# ---------------------------------------------------------------------------


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token -- the only form allowed in storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# A session token is `<base64url payload>.<base64url HMAC>` (see
# websocket.create_session_token). The payload encodes a JSON claims object so
# it is always well over 40 chars, and the SHA-256 signature is 43.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{20,}")
_TOKEN_HASH_SHAPE = re.compile(r"^[0-9a-f]{64}$")


def _looks_like_token(value: str) -> bool:
    return _TOKEN_SHAPE.search(value) is not None


def _assert_no_raw_token_in_audit_details(
    value: Any,
    *,
    token: str | None,
    path: str = "details",
) -> None:
    """Reject raw tokens anywhere inside free-form audit details."""
    if isinstance(value, str):
        if (token is not None and token in value) or _looks_like_token(value):
            raise ValueError(
                f"audit details field {path!r} looks like it contains a raw "
                "token; store a hash instead (ADR-0004 §3)"
            )
        return

    if isinstance(value, dict):
        for key, nested_value in value.items():
            key_path = f"{path}.{key}"
            _assert_no_raw_token_in_audit_details(key, token=token, path=key_path)
            _assert_no_raw_token_in_audit_details(
                nested_value, token=token, path=key_path
            )
        return

    if isinstance(value, (list, tuple, set)):
        for index, nested_value in enumerate(value):
            _assert_no_raw_token_in_audit_details(
                nested_value, token=token, path=f"{path}[{index}]"
            )


def record_audit(
    db: DBSession,
    *,
    action: str,
    actor_role: str,
    session_id: str | None = None,
    token: str | None = None,
    token_hash: str | None = None,
    details: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog:
    """Append an audit row (ADR-0004 §3).

    `token`, if given, is hashed immediately and discarded; the raw value is
    never written to the database. `token_hash` is accepted for revocation
    audits, where only the issuance audit's stored digest is available.

    `details` is free-form, which makes it the obvious place for a raw token to
    leak in by accident, so it is screened twice: against `token` itself when
    one was supplied, and -- always, including when no `token` argument was
    passed at all -- against the `<payload>.<signature>` shape a session token
    has. Either match raises rather than writing the row.

    Pass ``commit=False`` to enlist in the caller's transaction.
    """
    if token is not None and token_hash is not None:
        raise ValueError("pass either token or token_hash, not both")
    if token_hash is not None and _TOKEN_HASH_SHAPE.fullmatch(token_hash) is None:
        raise ValueError("token_hash must be a lowercase SHA-256 hex digest")

    safe_details = dict(details or {})
    _assert_no_raw_token_in_audit_details(safe_details, token=token)

    entry = AuditLog(
        session_id=session_id,
        action=action,
        actor_role=actor_role,
        token_hash=(
            token_hash
            if token_hash is not None
            else hash_token(token)
            if token is not None
            else None
        ),
        details=safe_details,
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    return entry


def issued_token_hashes(db: DBSession, session_id: str) -> list[str]:
    """Return token digests issued for a session, preserving issue order."""
    seen: set[str] = set()
    hashes: list[str] = []
    rows = db.scalars(
        select(AuditLog.token_hash)
        .where(AuditLog.session_id == session_id)
        .where(AuditLog.action == AUDIT_TOKEN_ISSUED)
        .where(AuditLog.token_hash.is_not(None))
        .order_by(AuditLog.id)
    ).all()
    for token_hash in rows:
        if token_hash is not None and token_hash not in seen:
            seen.add(token_hash)
            hashes.append(token_hash)
    return hashes


def revoked_token_hashes(db: DBSession, session_id: str) -> set[str]:
    """Return token digests already revoked for a session."""
    return {
        token_hash
        for token_hash in db.scalars(
            select(AuditLog.token_hash)
            .where(AuditLog.session_id == session_id)
            .where(AuditLog.action == AUDIT_TOKEN_REVOKED)
            .where(AuditLog.token_hash.is_not(None))
        )
        if token_hash is not None
    }


def is_token_hash_revoked(db: DBSession, session_id: str, token_hash: str) -> bool:
    """True when a token digest has a revocation audit row for this session."""
    return token_hash in revoked_token_hashes(db, session_id)


def revoke_session_tokens(
    db: DBSession,
    session_id: str,
    *,
    actor_role: str = "server",
    commit: bool = True,
) -> list[AuditLog]:
    """Audit-revoke all tokens previously issued for a session.

    Stateless HMAC tokens cannot be recovered from the database, so revocation
    is represented by appending ``token_revoked`` rows for each previously
    issued token hash. The WebSocket ASGI guard in ``main.py`` rejects any
    cookie whose digest appears in this set.
    """
    revoked = revoked_token_hashes(db, session_id)
    entries: list[AuditLog] = []
    for token_hash in issued_token_hashes(db, session_id):
        if token_hash in revoked:
            continue
        entries.append(
            record_audit(
                db,
                action=AUDIT_TOKEN_REVOKED,
                actor_role=actor_role,
                session_id=session_id,
                token_hash=token_hash,
                details={"reason": "session_ended"},
                commit=False,
            )
        )
        revoked.add(token_hash)
    if commit:
        db.commit()
    else:
        db.flush()
    return entries
