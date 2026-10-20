"""Recording completeness state machine (ADR-0005 §7, D8–D10).

One session has one MediaRecorder take. The student declares the end with
``POST /recording/{id}/finalize``. Until every sequence in ``0..final_sequence``
is stored and the header chunk validates, the take stays ``finalizing``. A
``recorder_lost`` or operator-marked incomplete take never becomes ``complete``,
so nothing here transcodes it or deletes its chunks.

Chunk deletion after a verified mp4 is intentionally not implemented: that
cleanup rides the later job queue. This module never unlinks a chunk or
``student_raw.webm``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session as DBSession

from . import auth, models
from .events import EventEnvelope, EventType, Source

logger = logging.getLogger(__name__)

# Recording rows did not exist before this deploy. The grace pass must not
# invent an ``incomplete`` / ``grace_expired`` row, or an audit entry, for a
# session that ended earlier and never had a take. ``/end`` creates a row for
# sessions it ends, and a missing row whose session ended at or after this
# instant is still synthesized so a zero-chunk end that did not go through
# ``/end`` (the websocket ``session_ended`` transition) can close.
UPLOAD_GRACE_SYNTHESIZE_ENDED_AFTER = datetime(2026, 10, 20, tzinfo=timezone.utc)

STUDENT_STATUS_KEYS = (
    "received_sequences",
    "next_expected_sequence",
    "final_sequence",
    "finalize_state",
    "recording_state",
)

#: Synchronous ``POST /process`` may run only from these states. Incomplete
#: takes, including ``recorder_lost``, are refused (ADR-0005 §7.2, D8).
TRANSCODE_STATES = frozenset(
    {"complete", "transcoding", "transcoded", "uploaded", "transcode_failed"}
)
TERMINAL_STATES = frozenset(
    {"transcoding", "transcoded", "uploaded", "transcode_failed", "deleting", "deleted"}
)

AUDIT_RECORDING_FINALIZED = "recording_finalized"
AUDIT_RECORDING_INCOMPLETE = "recording_incomplete"


def missing_for_final(received: list[int] | set[int], final_sequence: int) -> list[int]:
    """Sequences in ``0..final_sequence`` that are not stored.

    Unlike :func:`recording.missing_sequences`, the range starts at 0 even when
    the lowest stored sequence is higher, so a missing header is visible.
    """
    if final_sequence < 0:
        return []
    present = set(received)
    return [n for n in range(final_sequence + 1) if n not in present]


def next_expected_sequence(
    received: list[int], final_sequence: int | None
) -> int | None:
    """Smallest missing sequence, or None once a declared take has no gaps."""
    if final_sequence is not None:
        missing = missing_for_final(received, final_sequence)
        return missing[0] if missing else None
    if not received:
        return 0
    present = set(received)
    upper = max(present)
    for n in range(upper + 1):
        if n not in present:
            return n
    return upper + 1


def _received(session_id: str) -> list[int]:
    from . import recording

    return [sequence for sequence, _ in recording.stored_chunks(session_id)]


def _header_ok(session_id: str) -> bool:
    from . import recording

    return recording.validate_header_chunk(session_id)


def is_protected_incomplete(rec: models.Recording) -> bool:
    """D8: recorder_lost and operator incomplete are sticky.

    ``needs_operator`` counts too: an operator mark must not be cleared just
    because every chunk later arrives.
    """
    return bool(rec.needs_operator) or rec.incomplete_reason in models.PROTECTED_INCOMPLETE_REASONS


def completeness_ok(session_id: str, rec: models.Recording) -> bool:
    """True when the declared take is whole and the header chunk decodes.

    Requires ``total_chunks == final_sequence + 1``, the stored set to be
    exactly that many distinct sequences, no gaps in ``0..final_sequence``,
    and a valid header chunk. A repeated upload of the same sequence does not
    count twice.
    """
    if rec.final_sequence is None or rec.total_chunks is None:
        return False
    received = _received(session_id)
    if rec.total_chunks != rec.final_sequence + 1:
        return False
    if len(set(received)) != rec.total_chunks:
        return False
    if missing_for_final(received, rec.final_sequence):
        return False
    return _header_ok(session_id)


def _touch(rec: models.Recording) -> None:
    rec.updated_at = auth.utcnow()


def _sync_missing(session_id: str, rec: models.Recording) -> list[int]:
    if rec.final_sequence is None:
        missing: list[int] = []
    else:
        missing = missing_for_final(_received(session_id), rec.final_sequence)
    rec.missing_sequences = list(missing)
    return missing


def _mark_complete(rec: models.Recording) -> None:
    rec.recording_state = "complete"
    rec.finalize_state = "complete"
    rec.incomplete_reason = None
    rec.needs_operator = False
    rec.missing_sequences = []
    _touch(rec)


def refresh_completeness(
    db: DBSession, session_id: str, *, commit: bool = True
) -> models.Recording | None:
    """Re-check a take after a chunk lands or a duplicate finalize arrives.

    Protected incomplete takes only refresh ``missing_sequences``. They are
    not transcoded and their chunks are not removed.
    """
    rec = models.get_recording(db, session_id)
    if rec is None or rec.final_sequence is None:
        return rec
    if rec.recording_state in TERMINAL_STATES | {"deleting", "deleted"}:
        _sync_missing(session_id, rec)
        _touch(rec)
        if commit:
            db.commit()
        return rec
    # §7.1: incomplete is operator-only. A late chunk may shrink
    # missing_sequences, but it must not silently become complete.
    if rec.recording_state == "incomplete" or is_protected_incomplete(rec):
        _sync_missing(session_id, rec)
        _touch(rec)
        if commit:
            db.commit()
        return rec
    if rec.recording_state == "complete":
        return rec
    missing = _sync_missing(session_id, rec)
    if not missing and completeness_ok(session_id, rec):
        _mark_complete(rec)
    else:
        _touch(rec)
    if commit:
        db.commit()
    else:
        db.flush()
    return rec


def note_session_ended(db: DBSession, session_id: str) -> None:
    """``/end`` moves an in-progress take to ``finalizing`` (ADR-0005 §7.1).

    A session that never received a chunk still gets a row, so the grace check
    can mark it incomplete and the teacher status can see it. Does not finalize
    on the student's behalf and does not delete media. Caller owns the transaction.
    """
    rec = models.ensure_recording(db, session_id)
    if rec is None:
        return
    if rec.recording_state == "recording":
        rec.recording_state = "finalizing"
        _touch(rec)
        db.flush()


def _payload_matches(
    rec: models.Recording,
    *,
    final_sequence: int,
    total_chunks: int,
    total_duration_ms: int,
    reason: str | None,
) -> bool:
    if rec.final_sequence is None:
        return False
    return (
        rec.final_sequence == final_sequence
        and rec.total_chunks == total_chunks
        and rec.total_duration_ms == total_duration_ms
        and (rec.finalize_reason or None) == (reason or None)
    )


def _reject_conflict() -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "error": "finalize_conflict",
            "message": (
                "finalize payload does not match the one already stored "
                "for this recording"
            ),
        },
    )


def _apply_payload(
    rec: models.Recording,
    *,
    final_sequence: int,
    total_chunks: int,
    total_duration_ms: int,
    reason: str | None,
) -> None:
    rec.final_sequence = final_sequence
    rec.total_chunks = total_chunks
    rec.total_duration_ms = total_duration_ms
    rec.finalize_reason = reason
    _touch(rec)


def _audit(
    db: DBSession,
    *,
    action: str,
    session_id: str,
    actor_role: str,
    token_hash: str | None,
    details: dict[str, Any],
) -> None:
    models.record_audit(
        db,
        action=action,
        actor_role=actor_role,
        session_id=session_id,
        token_hash=token_hash,
        details=details,
        commit=False,
    )


def _announce_recorder_lost(db: DBSession, session_id: str) -> None:
    """Pause for ``recording_failure`` and tell live wizard/teacher sockets.

    The recording row is already incomplete. A session that cannot be paused
    (already ended, or still ``created``) keeps that incomplete row anyway.
    """
    session = models.get_session(db, session_id)
    if session is None or session.status not in {"active", "starting"}:
        return
    try:
        session = models.pause_session(
            db,
            session_id,
            reason="recording_failure",
            actor_role="server",
        )
    except ValueError:
        logger.warning(
            "session %s: recorder_lost could not pause from status %s",
            session_id,
            session.status,
        )
        return

    events = models.load_events(db, session_id)
    sequence = (
        max((event.sequence for event in events if event.source == Source.SERVER.value), default=-1)
        + 1
    )
    envelope = EventEnvelope(
        session_id=session_id,
        sequence=sequence,
        client_timestamp_ms=0,
        server_timestamp_ms=int(time.time() * 1000),
        clock_offset_ms=0,
        source=Source.SERVER,
        event_type=EventType.SESSION_PAUSED,
        payload={
            "event_id": f"server-recording-failure:{session_id}:{session.snapshot_revision}",
            "reason": "recording_failure",
            "auto_paused": True,
        },
    )
    try:
        models.append_event(db, session_id, envelope)
    except Exception:
        logger.exception(
            "session %s: recorder_lost pause is stored but the timeline event failed",
            session_id,
        )
        envelope_message = envelope.model_dump(mode="json")
    else:
        envelope_message = envelope.model_dump(mode="json")

    from . import websocket

    if session_id in websocket.manager._snapshots:
        snapshot = websocket.manager.snapshot_for(session_id)
        snapshot.session_state = "paused"
        snapshot.start_at_server_ms = None
        snapshot.bump()
        websocket._adopt_db_revision(snapshot, session.snapshot_revision)
    websocket.manager.schedule_broadcast(session_id, envelope_message)
    websocket.manager.mark_processed(session_id, str(envelope.payload["event_id"]))


def apply_finalize(
    db: DBSession,
    session_id: str,
    *,
    final_sequence: int,
    total_chunks: int,
    total_duration_ms: int,
    reason: str | None,
    actor_role: str,
    token_hash: str | None,
) -> dict[str, Any]:
    """Store a finalize declaration and return the current completeness view.

    The same payload is idempotent. A larger ``final_sequence`` is accepted
    while the take is still ``finalizing`` (the recorder flushed one more
    slice). Any other disagreement is 409. ``reason=recorder_lost`` latches
    ``incomplete`` immediately.
    """
    rec = models.ensure_recording(db, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown session")

    if rec.recording_state in {"deleting", "deleted"}:
        raise HTTPException(status_code=409, detail="recording_deleted")

    stored_above = sorted(
        sequence for sequence in _received(session_id) if sequence > final_sequence
    )
    if stored_above:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "final_sequence_below_stored",
                "message": "final_sequence is below chunks already stored",
                "final_sequence": final_sequence,
                "stored_above": stored_above,
            },
        )

    matches = _payload_matches(
        rec,
        final_sequence=final_sequence,
        total_chunks=total_chunks,
        total_duration_ms=total_duration_ms,
        reason=reason,
    )
    first_declaration = rec.final_sequence is None
    # An operator (or earlier recorder_lost) latch survives a later finalize.
    # The first declaration may record the sequence range; it cannot clear it.
    if is_protected_incomplete(rec):
        if not first_declaration and not matches:
            _reject_conflict()
        if first_declaration:
            _apply_payload(
                rec,
                final_sequence=final_sequence,
                total_chunks=total_chunks,
                total_duration_ms=total_duration_ms,
                reason=rec.finalize_reason or reason,
            )
        rec.recording_state = "incomplete"
        rec.finalize_state = "incomplete"
        rec.needs_operator = True
        _sync_missing(session_id, rec)
        _touch(rec)
        db.commit()
        return _finalize_body(rec)

    # grace_expired / missing_chunks stay incomplete. Recording the student's
    # declaration is fine; flipping back to complete is not (§7.1).
    if rec.recording_state == "incomplete":
        if not first_declaration and not matches:
            _reject_conflict()
        if first_declaration:
            _apply_payload(
                rec,
                final_sequence=final_sequence,
                total_chunks=total_chunks,
                total_duration_ms=total_duration_ms,
                reason=reason,
            )
        _sync_missing(session_id, rec)
        _touch(rec)
        db.commit()
        return _finalize_body(rec)

    if not matches and not first_declaration:
        if rec.recording_state in TERMINAL_STATES | {"complete", "incomplete"}:
            _reject_conflict()
        growing = (
            rec.recording_state == "finalizing"
            and final_sequence > (rec.final_sequence or -1)
        )
        lost_while_open = (
            reason == "recorder_lost"
            and rec.recording_state in {"recording", "finalizing"}
            and final_sequence >= (rec.final_sequence or -1)
        )
        if not growing and not lost_while_open:
            _reject_conflict()

    became_lost = reason == "recorder_lost" and rec.incomplete_reason != "recorder_lost"
    if not matches or first_declaration:
        _apply_payload(
            rec,
            final_sequence=final_sequence,
            total_chunks=total_chunks,
            total_duration_ms=total_duration_ms,
            reason=reason,
        )

    if reason == "recorder_lost":
        rec.recording_state = "incomplete"
        rec.finalize_state = "incomplete"
        rec.incomplete_reason = "recorder_lost"
        rec.needs_operator = True
        _sync_missing(session_id, rec)
        _touch(rec)
        if became_lost:
            _audit(
                db,
                action=AUDIT_RECORDING_INCOMPLETE,
                session_id=session_id,
                actor_role=actor_role,
                token_hash=token_hash,
                details={
                    "reason": "recorder_lost",
                    "final_sequence": final_sequence,
                    "missing_sequences": list(rec.missing_sequences or []),
                },
            )
            logger.warning(
                "session %s recording incomplete: recorder_lost final_sequence=%s missing=%s",
                session_id,
                final_sequence,
                list(rec.missing_sequences or []),
            )
        db.commit()
        if became_lost:
            try:
                _announce_recorder_lost(db, session_id)
            except Exception:
                logger.exception(
                    "session %s: recorder_lost is stored but the pause broadcast failed",
                    session_id,
                )
        return _finalize_body(rec)

    if rec.recording_state in {"recording", "finalizing"}:
        rec.recording_state = "finalizing"
        rec.finalize_state = "pending"
    _sync_missing(session_id, rec)
    if (
        not is_protected_incomplete(rec)
        and rec.recording_state not in TERMINAL_STATES | {"incomplete"}
        and completeness_ok(session_id, rec)
    ):
        _mark_complete(rec)
        if not matches or first_declaration:
            _audit(
                db,
                action=AUDIT_RECORDING_FINALIZED,
                session_id=session_id,
                actor_role=actor_role,
                token_hash=token_hash,
                details={
                    "final_sequence": rec.final_sequence,
                    "finalize_state": rec.finalize_state,
                },
            )
    elif not matches or first_declaration:
        _audit(
            db,
            action=AUDIT_RECORDING_FINALIZED,
            session_id=session_id,
            actor_role=actor_role,
            token_hash=token_hash,
            details={
                "final_sequence": rec.final_sequence,
                "finalize_state": rec.finalize_state,
                "missing_sequences": list(rec.missing_sequences or []),
            },
        )
    db.commit()
    return _finalize_body(rec)


def _finalize_body(rec: models.Recording) -> dict[str, Any]:
    return {
        "session_id": rec.session_id,
        "recording_state": rec.recording_state,
        "finalize_state": rec.finalize_state,
        "final_sequence": rec.final_sequence,
        "missing_sequences": list(rec.missing_sequences or []),
    }


def student_upload_status(session_id: str, db: DBSession) -> dict[str, Any]:
    """The five fields a student may see. No paths, sizes, or checksums."""
    received = _received(session_id)
    rec = models.get_recording(db, session_id)
    final_sequence = rec.final_sequence if rec is not None else None
    body = {
        "received_sequences": received,
        "next_expected_sequence": next_expected_sequence(received, final_sequence),
        "final_sequence": final_sequence,
        "finalize_state": rec.finalize_state if rec is not None else "none",
        "recording_state": rec.recording_state if rec is not None else "recording",
    }
    assert set(body) == set(STUDENT_STATUS_KEYS)
    return body


def transcode_allowed(session_id: str, db: DBSession) -> bool:
    """Existing ``POST /process`` runs only for a complete, unprotected take."""
    rec = models.get_recording(db, session_id)
    if rec is None or rec.recording_state not in TRANSCODE_STATES:
        return False
    if is_protected_incomplete(rec):
        return False
    return completeness_ok(session_id, rec)


def mark_transcode_started(db: DBSession, session_id: str) -> None:
    rec = models.get_recording(db, session_id)
    if rec is None:
        return
    rec.recording_state = "transcoding"
    _touch(rec)
    db.commit()


def mark_transcode_result(db: DBSession, session_id: str, *, status: str) -> None:
    """Record the outcome of the existing synchronous transcode. Never deletes media."""
    rec = models.get_recording(db, session_id)
    if rec is None or is_protected_incomplete(rec):
        return
    if status == "completed":
        rec.recording_state = "transcoded"
    elif status == "skipped":
        rec.recording_state = "complete"
    elif status == "raw_only" and rec.recording_state == "transcoding":
        rec.recording_state = "transcode_failed"
    _touch(rec)
    db.commit()


def mark_operator_incomplete(
    db: DBSession, session_id: str, *, actor_role: str = "operator"
) -> models.Recording:
    """Latch an operator incomplete mark. Later chunks cannot clear it (D8)."""
    rec = models.ensure_recording(db, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown session")
    rec.recording_state = "incomplete"
    rec.finalize_state = "incomplete"
    rec.incomplete_reason = "operator_incomplete"
    rec.needs_operator = True
    _sync_missing(session_id, rec)
    _touch(rec)
    _audit(
        db,
        action=AUDIT_RECORDING_INCOMPLETE,
        session_id=session_id,
        actor_role=actor_role,
        token_hash=None,
        details={"reason": "operator_incomplete"},
    )
    db.commit()
    return rec


def claim_open_recording(
    db: DBSession, session_id: str, values: dict[str, Any]
) -> bool:
    """Move a still-open take, or change nothing if it left that state.

    The predicate is ``recording_state IN ('recording', 'finalizing') AND
    needs_operator = 0``. A concurrent ``recorder_lost`` or operator incomplete
    update fails the predicate, so this write cannot replace ``incomplete_reason``.
    """
    result = db.execute(
        update(models.Recording)
        .where(models.Recording.session_id == session_id)
        .where(models.Recording.recording_state.in_(("recording", "finalizing")))
        .where(models.Recording.needs_operator.is_(False))
        .values(**values)
    )
    return result.rowcount == 1


def apply_upload_grace_deadlines(
    db: DBSession, *, now: Any | None = None
) -> list[str]:
    """Close takes whose post-end grace window has elapsed (ADR-0005 §7.3).

    Protected incomplete takes are skipped. A finalized take with no gaps
    becomes ``complete``; anything else becomes ``incomplete``. A session that
    never stored a chunk and ended at or after
    ``UPLOAD_GRACE_SYNTHESIZE_ENDED_AFTER`` is created as ``incomplete`` /
    ``grace_expired``. Older ended sessions with no recording row are left
    alone so the first pass after deploy does not rewrite history. Raw chunks
    are left on disk either way. Returns the session ids whose state changed.
    """
    from sqlalchemy import select

    moment = now or auth.utcnow()
    if getattr(moment, "tzinfo", None) is None:
        moment = moment.replace(tzinfo=auth.utcnow().tzinfo)
    grace = timedelta(seconds=auth.upload_grace_seconds())
    changed: list[str] = []
    sessions = db.scalars(
        select(models.Session).where(models.Session.status == "ended")
    ).all()
    for session in sessions:
        if session.ended_at is None:
            continue
        ended = session.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=moment.tzinfo)
        if moment < ended + grace:
            continue
        rec = models.get_recording(db, session.id)
        created = False
        if rec is None:
            if ended < UPLOAD_GRACE_SYNTHESIZE_ENDED_AFTER:
                continue
            rec = models.ensure_recording(db, session.id)
            created = rec is not None
        if rec is None or rec.recording_state not in {"recording", "finalizing"}:
            continue
        if is_protected_incomplete(rec):
            continue
        missing = (
            missing_for_final(_received(session.id), rec.final_sequence)
            if rec.final_sequence is not None
            else []
        )
        if rec.final_sequence is not None and completeness_ok(session.id, rec):
            outcome = "complete"
            values: dict[str, Any] = {
                "recording_state": "complete",
                "finalize_state": "complete",
                "incomplete_reason": None,
                "needs_operator": False,
                "missing_sequences": [],
                "updated_at": moment,
            }
        else:
            outcome = "grace_expired" if rec.final_sequence is None else "missing_chunks"
            values = {
                "recording_state": "incomplete",
                "finalize_state": "incomplete",
                "incomplete_reason": outcome,
                "missing_sequences": missing,
                "updated_at": moment,
            }
        if not claim_open_recording(db, session.id, values):
            # Lost the race with recorder_lost / operator incomplete, or the
            # row already left recording|finalizing. Drop a row this pass just
            # inserted so a failed claim does not leave an empty 'recording'.
            if created:
                db.delete(rec)
                db.flush()
            else:
                db.expire(rec)
            continue
        db.expire(rec)
        if outcome != "complete":
            _audit(
                db,
                action=AUDIT_RECORDING_INCOMPLETE,
                session_id=session.id,
                actor_role="server",
                token_hash=None,
                details={
                    "reason": outcome,
                    "missing_sequences": missing,
                },
            )
            logger.warning(
                "session %s recording incomplete after upload grace: %s missing=%s",
                session.id,
                outcome,
                missing,
            )
        changed.append(session.id)
        logger.info(
            "session %s upload grace resolved to %s", session.id, outcome
        )
    if changed:
        db.commit()
    return changed
