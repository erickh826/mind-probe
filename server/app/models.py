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
import logging
import os
import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    String,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    inspect,
    or_,
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

logger = logging.getLogger(__name__)

# ``websocket.py`` registers this so a revocation commit can close live
# sockets. models does not import the realtime module (that would cycle).
RevocationListener = Callable[[str, str | None, str], None]
_revocation_listeners: list[RevocationListener] = []


def add_revocation_listener(listener: RevocationListener) -> None:
    """Notify ``listener`` after a revocation is committed.

    The callback is ``(session_id, token_hash, reason)``. ``token_hash`` is
    None when every issued token for the session was revoked. Listeners are
    skipped when the caller passes ``commit=False`` and commits later itself.
    """
    if listener not in _revocation_listeners:
        _revocation_listeners.append(listener)


def _notify_revocation(session_id: str, token_hash: str | None, reason: str) -> None:
    for listener in list(_revocation_listeners):
        try:
            listener(session_id, token_hash, reason)
        except Exception:
            logger.exception(
                "revocation listener failed for session %s", session_id
            )

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
AUDIT_JOIN_CODES_REISSUED = "join_codes_reissued"
AUDIT_SESSION_PAUSED = "session_paused"
AUDIT_CONNECTION_TAKEOVER = "connection_takeover"
AUDIT_UPLOAD_CODE_ISSUED = "upload_code_issued"
AUDIT_UPLOAD_TOKEN_ISSUED = "upload_token_issued"
AUDIT_REVIEW_LOGIN = "review_login"
AUDIT_REVIEW_REFRESH = "review_refresh"
AUDIT_REVIEW_LOGOUT = "review_logout"
AUDIT_REVIEW_REVOKE = "review_revoke"

#: Reusable review codes issued to the operator (ADR-0005 §6.5). Not join codes.
REVIEW_CODE_ROLES = ("teacher", "observer")

#: Plaintext upload codes live for ten minutes. Only the SHA-256 digest is stored.
UPLOAD_CODE_TTL_SECONDS = 10 * 60


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


class UploadGrant(Base):
    """Session-level upload credential state.

    One row per session. ``security_revoked_at`` is set in the same transaction
    as a security revocation and is checked when an upload code is issued,
    redeemed, or used. ``POST /sessions/{id}/end`` does not set it, so grace
    uploads keep working.
    """

    __tablename__ = "upload_grants"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    security_revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class UploadCode(Base):
    """One-time upload code. The raw code is returned once and never stored."""

    __tablename__ = "upload_codes"
    __table_args__ = (UniqueConstraint("code_hash", name="uq_upload_codes_code_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class UploadCodeInvalid(Exception):
    """The code does not exist for this session."""


class UploadCodeExpired(Exception):
    """The code's ten-minute lifetime has elapsed. It is left unconsumed."""


class UploadCodeConsumed(Exception):
    """The code was already redeemed or withdrawn by a later reissue."""


class ReviewCode(Base):
    """Reusable teacher/observer review code. The raw code is never stored."""

    __tablename__ = "review_codes"
    __table_args__ = (
        UniqueConstraint("session_id", "role", name="uq_review_codes_session_role"),
        UniqueConstraint("code_hash", name="uq_review_codes_code_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String, index=True)
    code_hash: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ReviewCodeInvalid(Exception):
    """The review code does not match a hash stored for this session."""


class ReviewGrant(Base):
    """One review access/refresh pair (ADR-0005 §6.5).

    ``expires_at`` is the refresh-token deadline. The access token carries its
    own shorter ``exp``. ``rotated_from`` points at the grant whose refresh
    token was exchanged for this row. SQLite UNIQUE allows many NULL parents,
    so each login can start its own chain.
    """

    __tablename__ = "review_grants"

    jti: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    client_id: Mapped[str] = mapped_column(String)
    refresh_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rotated_from: Mapped[str | None] = mapped_column(
        ForeignKey("review_grants.jti"), nullable=True, unique=True
    )


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


#: ``recordings.recording_state`` (ADR-0005 §7.1). Later states are stored so
#: the machine can name them; this batch only transitions through recording,
#: finalizing, complete, incomplete, and the existing synchronous transcode.
RECORDING_STATES = (
    "recording",
    "finalizing",
    "complete",
    "incomplete",
    "transcoding",
    "transcoded",
    "uploaded",
    "transcode_failed",
    "deleting",
    "deleted",
)
FINALIZE_STATES = ("none", "pending", "complete", "incomplete")
#: D8: these incomplete reasons are sticky. A full chunk set must not flip them
#: back to complete, so nothing auto-transcodes or deletes their raw media.
PROTECTED_INCOMPLETE_REASONS = frozenset({"recorder_lost", "operator_incomplete"})


class Recording(Base):
    """One MediaRecorder take for a session (ADR-0005 §7).

    Phase 1 is a single take per session. ``next_sequence`` lives in the
    browser; this row is the server's view of how far that take got.
    """

    __tablename__ = "recordings"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), primary_key=True
    )
    recording_state: Mapped[str] = mapped_column(String, default="recording")
    finalize_state: Mapped[str] = mapped_column(String, default="none")
    final_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_chunks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ``recorder_lost`` when the student declared the take dead, else None.
    finalize_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    incomplete_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    needs_operator: Mapped[bool] = mapped_column(Boolean, default=False)
    missing_sequences: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    recording_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Job(Base):
    """Durable, single-worker jobs; no URLs or credentials in payloads."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)
    session_id: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="queued")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int | None] = mapped_column(Integer, default=3, nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    __table_args__ = (
        Index("uq_active_job", "kind", "session_id", unique=True,
              sqlite_where=status.in_(("queued", "running"))),
    )


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

    ``create_all`` creates missing tables; this batch additively migrates the
    five recording-storage/deletion columns on existing SQLite databases.
    No sessions, original media or audit records are removed by startup.
    """
    os.makedirs("./sessions", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # Additive migration: never require deleting an existing research database.
    if engine.dialect.name == "sqlite":
        additions = {
            "recording_key": "TEXT", "recording_sha256": "TEXT", "manifest": "JSON",
            "deleted_by": "TEXT", "deleted_at": "DATETIME",
        }
        with engine.begin() as conn:
            columns = {c["name"] for c in inspect(conn).get_columns("recordings")}
            for name, sql_type in additions.items():
                if name not in columns:
                    conn.exec_driver_sql(f"ALTER TABLE recordings ADD COLUMN {name} {sql_type}")


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


def list_sessions(
    db: DBSession, *, limit: int, before: tuple[datetime, str] | None = None
) -> list[Session]:
    """Newest sessions first, for the operator page.

    ``before`` is the ``(created_at, id)`` of the last row already shown; rows
    strictly after it in this order come next, so ties on ``created_at`` and
    sessions created between pages neither repeat nor go missing.
    """
    stmt = select(Session)
    if before is not None:
        created_at, session_id = before
        stmt = stmt.where(
            or_(
                Session.created_at < created_at,
                and_(Session.created_at == created_at, Session.id < session_id),
            )
        )
    stmt = stmt.order_by(Session.created_at.desc(), Session.id.desc()).limit(limit)
    return list(db.scalars(stmt))


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
    reusable = join_code.role in ("teacher", "observer")
    if join_code.consumed_at is not None and not reusable:
        raise JoinCodeConsumed("join code already consumed")
    # Every claim, including a reusable code's repeat use, is an UPDATE that
    # still matches this code's hash. A concurrent ``reissue_join_codes`` that
    # replaced the hash after the SELECT above turns the claim into an invalid
    # code instead of letting the old code claim the new row.
    claim = (
        update(JoinCode)
        .where(JoinCode.id == join_code.id)
        .where(JoinCode.code_hash == code_hash)
    )
    if join_code.consumed_at is None:
        consumed_at = _utcnow()
        claim = claim.where(JoinCode.consumed_at.is_(None)).values(consumed_at=consumed_at)
    else:
        consumed_at = join_code.consumed_at
        claim = claim.values(consumed_at=JoinCode.consumed_at)
    result = db.execute(claim)
    if result.rowcount != 1:
        db.rollback()
        still_valid = db.scalars(
            select(JoinCode.id)
            .where(JoinCode.session_id == session_id)
            .where(JoinCode.code_hash == code_hash)
        ).first()
        if still_valid is None:
            raise JoinCodeInvalid("invalid join code")
        raise JoinCodeConsumed("join code already consumed")
    join_code.consumed_at = consumed_at
    if commit:
        db.commit()
    else:
        db.flush()
    return join_code


_JOIN_CODE_REPLACE_ATTEMPTS = 3


def _replace_join_code(
    db: DBSession, session_id: str, role: str, code_hash: str, now: datetime
) -> str | None:
    """Swap one role's code hash in place and return the hash it replaced.

    The UPDATE names every column it resets. An ORM assignment of
    ``consumed_at = None`` would be dropped when the loaded value was already
    None, keeping the timestamp of a join that committed after the read, and
    the new code would start out consumed. The UPDATE is also guarded on the
    hash it read, so a concurrent reissue makes this retry instead of
    recording the wrong old hash.
    """
    for _ in range(_JOIN_CODE_REPLACE_ATTEMPTS):
        current = db.execute(
            select(JoinCode.id, JoinCode.code_hash)
            .where(JoinCode.session_id == session_id)
            .where(JoinCode.role == role)
            .with_for_update()
        ).first()
        if current is None:
            # Sessions from before join codes were stored per role.
            db.add(
                JoinCode(session_id=session_id, role=role, code_hash=code_hash, created_at=now)
            )
            return None
        result = db.execute(
            update(JoinCode)
            .where(JoinCode.id == current.id)
            .where(JoinCode.code_hash == current.code_hash)
            .values(code_hash=code_hash, consumed_at=None, created_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            return current.code_hash
    raise RuntimeError(f"join code for {role} kept changing during reissue")


def reissue_join_codes(
    db: DBSession,
    session_id: str,
    roles: tuple[str, ...],
    *,
    revoke_live: bool,
) -> tuple[dict[str, str], list[str]]:
    """Replace the join codes for ``roles``; with ``revoke_live``, revoke their live tokens.

    Enlists in the caller's transaction and does not commit. The new hashes,
    the cleared ``consumed_at``, every ``token_revoked`` row and the
    ``join_codes_reissued`` audit land together or not at all. Returns the raw
    codes, shown to the operator once, and the revoked token digests. The
    caller closes those sockets only after its commit succeeds.

    Revocation covers only live tokens that ``/join`` issued for these roles
    (their ``token_issued`` audits). Review grants, upload tokens and the
    session's upload grant are untouched, and the reason is never
    ``security``, so upload recovery stays available.
    """
    _require_session(db, session_id)
    now = _utcnow()
    codes: dict[str, str] = {}
    old_hashes: dict[str, str | None] = {}
    new_hashes: dict[str, str] = {}
    for role in roles:
        if role not in JOIN_CODE_ROLES:
            raise ValueError(f"invalid join-code role: {role}")
        raw_code = secrets.token_urlsafe(24)
        code_hash = hash_join_code(raw_code)
        old_hashes[role] = _replace_join_code(db, session_id, role, code_hash, now)
        codes[role] = raw_code
        new_hashes[role] = code_hash
    # The new hashes are written before issued tokens are read, so a join that
    # committed first is seen and one that commits later finds its code gone.
    db.flush()

    revoked: list[str] = []
    if revoke_live:
        already = revoked_token_hashes(db, session_id)
        for token_hash in issued_token_hashes(db, session_id, roles=roles):
            if token_hash in already:
                continue
            record_audit(
                db,
                action=AUDIT_TOKEN_REVOKED,
                actor_role="operator",
                session_id=session_id,
                token_hash=token_hash,
                details={"reason": REVOKE_REASON_JOIN_CODES_REISSUED},
                commit=False,
            )
            already.add(token_hash)
            revoked.append(token_hash)
    record_audit(
        db,
        action=AUDIT_JOIN_CODES_REISSUED,
        actor_role="operator",
        session_id=session_id,
        details={
            "roles": list(roles),
            "old_code_hashes": old_hashes,
            "new_code_hashes": new_hashes,
            "revoke_live": revoke_live,
            "revoked_tokens": len(revoked),
        },
        commit=False,
    )
    return codes, revoked


def upload_security_revoked(db: DBSession, session_id: str) -> bool:
    """True when this session's upload grant was closed for security."""
    grant = db.get(UploadGrant, session_id)
    return grant is not None and grant.security_revoked_at is not None


def ensure_upload_grant(db: DBSession, session_id: str) -> UploadGrant:
    """Return the session's upload grant, creating it if this is the first code."""
    _require_session(db, session_id)
    grant = db.get(UploadGrant, session_id)
    if grant is None:
        grant = UploadGrant(session_id=session_id)
        db.add(grant)
        db.flush()
    return grant


def mark_upload_grant_security_revoked(
    db: DBSession, session_id: str, *, commit: bool = False
) -> UploadGrant | None:
    """Latch ``security_revoked_at`` for a security revocation of this session.

    No row is written when the session itself is already gone. ``/end`` does
    not call this.
    """
    if get_session(db, session_id) is None:
        return None
    grant = db.get(UploadGrant, session_id)
    if grant is None:
        grant = UploadGrant(session_id=session_id, security_revoked_at=_utcnow())
        db.add(grant)
    elif grant.security_revoked_at is None:
        grant.security_revoked_at = _utcnow()
    if commit:
        db.commit()
    else:
        db.flush()
    return grant


def issue_upload_code(db: DBSession, session_id: str, *, expires_at: datetime) -> str:
    """Mint one upload code and withdraw every older unused code for the session.

    The raw code is returned to the caller. The database stores only its hash.
    The caller commits.
    """
    ensure_upload_grant(db, session_id)
    withdrawn_at = _utcnow()
    db.execute(
        update(UploadCode)
        .where(UploadCode.session_id == session_id)
        .where(UploadCode.consumed_at.is_(None))
        .values(consumed_at=withdrawn_at)
    )
    raw_code = secrets.token_urlsafe(24)
    db.add(
        UploadCode(
            session_id=session_id,
            code_hash=hash_join_code(raw_code),
            expires_at=expires_at,
        )
    )
    db.flush()
    return raw_code


def consume_upload_code(db: DBSession, session_id: str, code: str, *, now: datetime) -> UploadCode:
    """Redeem one code for this session. A second concurrent redeem loses.

    Lookup is scoped to ``session_id``, so a code for another session is
    ``UploadCodeInvalid`` and is not consumed. An expired code is not consumed.
    """
    code_hash = hash_join_code(code)
    upload_code = db.scalars(
        select(UploadCode)
        .where(UploadCode.session_id == session_id)
        .where(UploadCode.code_hash == code_hash)
        .limit(1)
    ).first()
    if upload_code is None:
        raise UploadCodeInvalid("invalid upload code")
    expires_at = upload_code.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if current >= expires_at:
        raise UploadCodeExpired("upload code expired")
    if upload_code.consumed_at is not None:
        raise UploadCodeConsumed("upload code already consumed")
    consumed_at = _utcnow()
    result = db.execute(
        update(UploadCode)
        .where(UploadCode.id == upload_code.id)
        .where(UploadCode.consumed_at.is_(None))
        .values(consumed_at=consumed_at)
    )
    if result.rowcount != 1:
        db.rollback()
        raise UploadCodeConsumed("upload code already consumed")
    upload_code.consumed_at = consumed_at
    db.flush()
    return upload_code


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
# Recording chunk metadata (spec §10.1, ADR-0004 §4)
# ---------------------------------------------------------------------------


def get_recording_chunks(db: DBSession, session_id: str) -> list[RecordingChunk]:
    """Return a session's chunk metadata ordered by ``sequence``.

    ``id`` breaks ties so the order is deterministic even if a sequence was
    somehow recorded twice.
    """
    return list(
        db.scalars(
            select(RecordingChunk)
            .where(RecordingChunk.session_id == session_id)
            .order_by(RecordingChunk.sequence, RecordingChunk.id)
        )
    )


def save_recording_chunk_metadata(
    db: DBSession,
    session_id: str,
    sequence: int,
    *,
    size_bytes: int | None = None,
    checksum: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    path: str | None = None,
    upload_status: str = "received",
    commit: bool = True,
) -> RecordingChunk:
    """Upsert the metadata row for one uploaded chunk.

    Upload retries are normal (ADR-0002 §8), so a second call for the same
    ``(session_id, sequence)`` refreshes the existing row rather than adding a
    duplicate. The uniqueness is enforced here rather than by a
    ``UniqueConstraint`` on the table: as :func:`init_db` notes, ``create_all``
    does not alter existing tables and there is no migration tool wired up, so
    a new constraint would apply to fresh databases and silently not to the
    ``sessions/mind-probe.sqlite`` already on disk -- the worst of both.

    Unlike events, chunk metadata *is* mutable: it describes a file whose
    bytes are the real artefact, and ADR-0004 §4's immutability applies to the
    raw event log and the media, not to this index over it.
    """
    existing = db.scalars(
        select(RecordingChunk)
        .where(RecordingChunk.session_id == session_id)
        .where(RecordingChunk.sequence == sequence)
        .order_by(RecordingChunk.id)
        .limit(1)
    ).first()

    chunk = existing or RecordingChunk(session_id=session_id, sequence=sequence)
    chunk.size_bytes = size_bytes
    chunk.checksum = checksum
    chunk.start_ms = start_ms
    chunk.end_ms = end_ms
    chunk.path = path
    chunk.upload_status = upload_status
    if existing is None:
        db.add(chunk)

    if commit:
        db.commit()
    else:
        db.flush()
    return chunk


def get_recording(db: DBSession, session_id: str) -> Recording | None:
    """Return the recording row for a session, or None when ingest never started."""
    return db.get(Recording, session_id)


def ensure_recording(db: DBSession, session_id: str) -> Recording | None:
    """Create the recording row on first ingest. Unknown sessions stay row-less.

    Chunk bytes are still stored when the session row is missing (ADR-0004 §4);
    the state machine cannot reference a session that does not exist.
    """
    existing = get_recording(db, session_id)
    if existing is not None:
        return existing
    if get_session(db, session_id) is None:
        return None
    recording = Recording(session_id=session_id, missing_sequences=[])
    db.add(recording)
    db.flush()
    return recording


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


def issued_token_hashes(
    db: DBSession, session_id: str, *, roles: tuple[str, ...] | None = None
) -> list[str]:
    """Return live token digests issued for a session, preserving issue order.

    ``roles`` narrows to the tokens ``/join`` issued for those roles.
    """
    seen: set[str] = set()
    hashes: list[str] = []
    stmt = (
        select(AuditLog.token_hash)
        .where(AuditLog.session_id == session_id)
        .where(AuditLog.action == AUDIT_TOKEN_ISSUED)
        .where(AuditLog.token_hash.is_not(None))
    )
    if roles is not None:
        stmt = stmt.where(AuditLog.actor_role.in_(roles))
    rows = db.scalars(stmt.order_by(AuditLog.id)).all()
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
    issued token hash. The WebSocket upgrade rejects any token whose digest
    appears in this set, and a committed revocation closes live sockets.
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
        _notify_revocation(session_id, None, "session_ended")
    else:
        db.flush()
    return entries


REVOKE_REASON_SESSION_ENDED = "session_ended"
REVOKE_REASON_SECURITY = "security"
#: OP replaced a role's join code and chose to cut that role's live tokens.
#: Exempt nowhere: no ended exemption and no grace upload.
REVOKE_REASON_JOIN_CODES_REISSUED = "join_codes_reissued"


def token_revocation_reason(
    db: DBSession, session_id: str, token_hash: str
) -> str | None:
    """Why a token digest was revoked, or None when it is not revoked (ADR-0005 §1)."""
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.session_id == session_id)
        .where(AuditLog.action == AUDIT_TOKEN_REVOKED)
        .where(AuditLog.token_hash == token_hash)
        .order_by(AuditLog.id)
    ).all()
    if not rows:
        return None
    # Any security revocation wins, and any other non-ended reason wins over
    # session_ended, so a stricter revocation never inherits the ended exemption.
    reasons = {(row.details or {}).get("reason") for row in rows}
    if REVOKE_REASON_SECURITY in reasons:
        return REVOKE_REASON_SECURITY
    stricter = sorted(str(reason) for reason in reasons if reason != REVOKE_REASON_SESSION_ENDED)
    return stricter[0] if stricter else REVOKE_REASON_SESSION_ENDED


def notify_revocations(session_id: str, token_hashes: list[str]) -> None:
    """Close the sockets of exactly these token digests. Call after the commit."""
    for token_hash in token_hashes:
        # Close reason is the wire code, not the audit detail.
        _notify_revocation(session_id, token_hash, "token_revoked")


def revoke_token_hash(
    db: DBSession,
    session_id: str,
    token_hash: str,
    *,
    reason: str = REVOKE_REASON_SECURITY,
    actor_role: str = "server",
    commit: bool = True,
) -> AuditLog:
    """Revoke a single token digest, e.g. after a leak (ADR-0005 §1).

    A security reason also latches ``upload_grants.security_revoked_at`` in
    this same transaction, so a later upload code or token cannot bypass it.
    """
    if reason == REVOKE_REASON_SECURITY:
        mark_upload_grant_security_revoked(db, session_id, commit=False)
    entry = record_audit(
        db,
        action=AUDIT_TOKEN_REVOKED,
        actor_role=actor_role,
        session_id=session_id,
        token_hash=token_hash,
        details={"reason": reason},
        commit=False,
    )
    if commit:
        db.commit()
        # Close reason is the wire code, not the audit detail ("security").
        _notify_revocation(session_id, token_hash, "token_revoked")
    return entry


# ---------------------------------------------------------------------------
# Review grants (ADR-0005 §6.5). Separate from upload grants and live tokens.
# ---------------------------------------------------------------------------


def _dt_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def issue_review_codes(db: DBSession, session_id: str, *, commit: bool = True) -> dict[str, str]:
    """Issue a fresh teacher code and observer code, replacing any previous hashes.

    Raw codes are returned once. Regenerating does not revoke review grants
    that were already minted from the old codes.
    """
    _require_session(db, session_id)
    issued: dict[str, str] = {}
    now = _utcnow()
    for role in REVIEW_CODE_ROLES:
        raw_code = secrets.token_urlsafe(24)
        digest = hash_join_code(raw_code)
        row = db.scalars(
            select(ReviewCode)
            .where(ReviewCode.session_id == session_id)
            .where(ReviewCode.role == role)
            .limit(1)
        ).first()
        if row is None:
            db.add(
                ReviewCode(
                    session_id=session_id,
                    role=role,
                    code_hash=digest,
                    created_at=now,
                )
            )
        else:
            row.code_hash = digest
            row.created_at = now
        issued[role] = raw_code
    if commit:
        db.commit()
    else:
        db.flush()
    return issued


def authenticate_review_code(db: DBSession, session_id: str, code: str) -> ReviewCode:
    """Resolve a reusable review code to its role. Does not consume it."""
    code_hash = hash_join_code(code)
    row = db.scalars(
        select(ReviewCode)
        .where(ReviewCode.session_id == session_id)
        .where(ReviewCode.code_hash == code_hash)
        .limit(1)
    ).first()
    if row is None:
        raise ReviewCodeInvalid("invalid review code")
    return row


def insert_review_grant(
    db: DBSession,
    *,
    jti: str,
    session_id: str,
    role: str,
    client_id: str,
    refresh_hash: str,
    issued_at: datetime,
    expires_at: datetime,
    rotated_from: str | None = None,
) -> ReviewGrant:
    """Insert one grant. The caller commits, including the audit row."""
    grant = ReviewGrant(
        jti=jti,
        session_id=session_id,
        role=role,
        client_id=client_id,
        refresh_hash=refresh_hash,
        issued_at=issued_at,
        expires_at=expires_at,
        rotated_from=rotated_from,
    )
    db.add(grant)
    db.flush()
    return grant


def review_grant_by_refresh_hash(db: DBSession, refresh_hash: str) -> ReviewGrant | None:
    return db.scalars(
        select(ReviewGrant).where(ReviewGrant.refresh_hash == refresh_hash).limit(1)
    ).first()


def review_grant_successor(db: DBSession, jti: str) -> ReviewGrant | None:
    return db.scalars(
        select(ReviewGrant).where(ReviewGrant.rotated_from == jti).limit(1)
    ).first()


def is_review_grant_revoked(db: DBSession, jti: str) -> bool:
    """True when this jti is missing, revoked, or past its refresh expiry."""
    grant = db.get(ReviewGrant, jti)
    if grant is None or grant.revoked_at is not None:
        return True
    return _dt_aware(grant.expires_at) <= _utcnow()


def review_access_block(
    db: DBSession, jti: str | None, *, session_id: str, role: str
) -> str | None:
    """Why a review access token must be refused, or None when the grant is live.

    Return values: ``revoked``, ``expired``, ``session``, ``role``.
    """
    if not jti:
        return "revoked"
    grant = db.get(ReviewGrant, jti)
    if grant is None or grant.revoked_at is not None:
        return "revoked"
    if _dt_aware(grant.expires_at) <= _utcnow():
        return "expired"
    if grant.session_id != session_id:
        return "session"
    if grant.role != role:
        return "role"
    return None


def revoke_review_chain(db: DBSession, jti: str) -> list[str]:
    """Revoke ``jti`` and every grant in its rotation chain. Caller commits."""
    start = db.get(ReviewGrant, jti)
    if start is None:
        return []
    root = start
    guard: set[str] = set()
    while root.rotated_from and root.rotated_from not in guard:
        guard.add(root.jti)
        parent = db.get(ReviewGrant, root.rotated_from)
        if parent is None or parent.session_id != start.session_id:
            break
        root = parent
    rows = db.scalars(
        select(ReviewGrant).where(ReviewGrant.session_id == root.session_id)
    ).all()
    children: dict[str, list[ReviewGrant]] = {}
    for row in rows:
        if row.rotated_from:
            children.setdefault(row.rotated_from, []).append(row)
    ordered: list[ReviewGrant] = []
    stack = [root]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node.jti in seen:
            continue
        seen.add(node.jti)
        ordered.append(node)
        stack.extend(children.get(node.jti, []))
    now = _utcnow()
    for node in ordered:
        if node.revoked_at is None:
            node.revoked_at = now
    db.flush()
    return [node.jti for node in ordered]


def revoke_all_review_grants(db: DBSession, session_id: str) -> list[str]:
    """Revoke every live review grant for a session. Caller commits.

    Does not touch upload grants or live join tokens.
    """
    rows = db.scalars(
        select(ReviewGrant).where(ReviewGrant.session_id == session_id)
    ).all()
    now = _utcnow()
    revoked: list[str] = []
    for row in rows:
        if row.revoked_at is None:
            row.revoked_at = now
            revoked.append(row.jti)
    db.flush()
    return revoked
