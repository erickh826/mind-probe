"""Durable SQLite queue and one execution worker (ADR-0005 sections 4-6)."""
from __future__ import annotations

import gzip
import logging
import math
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from . import models, storage

logger = logging.getLogger(__name__)
DELETE_KINDS = frozenset({"delete_recording", "delete_orphan_object"})
KINDS = DELETE_KINDS | {"transcode", "upload", "finalize_timeout", "sqlite_backup"}
ACTIVE = ("queued", "running")
TOMBSTONES = ("deleting", "deleted")
BACKOFF = (60, 300, 900)


def now_utc() -> datetime:
    # SQLite stores naive UTC; compare only normalized values in queue code.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def enqueue(db, kind: str, session_id: str = "", *, payload: dict | None = None,
            run_after: datetime | None = None) -> int:
    """Caller owns the transaction. SQLite conflict handling never rolls it back."""
    if kind not in KINDS:
        raise ValueError("unknown job kind")
    moment = now_utc()
    stmt = insert(models.Job).values(
        kind=kind, session_id=session_id, payload=payload or {}, status="queued",
        attempts=0, max_attempts=None if kind in DELETE_KINDS else 3,
        run_after=naive(run_after) if run_after else moment,
        created_at=moment, updated_at=moment,
    ).on_conflict_do_nothing(
        index_elements=["kind", "session_id"],
        index_where=models.Job.status.in_(ACTIVE),
    ).returning(models.Job.id)
    job_id = db.scalar(stmt)
    if job_id is None:
        job_id = db.scalar(select(models.Job.id).where(
            models.Job.kind == kind, models.Job.session_id == session_id,
            models.Job.status.in_(ACTIVE)))
    return int(job_id)


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    session_id: str
    payload: dict


def claim(db, worker_id: str, *, now: datetime | None = None) -> ClaimedJob | None:
    moment = now or now_utc()
    candidate = select(models.Job.id).where(
        models.Job.status == "queued", models.Job.run_after <= moment,
    ).order_by(models.Job.id).limit(1).scalar_subquery()
    job_id = db.scalar(update(models.Job).where(
        models.Job.id == candidate, models.Job.status == "queued",
    ).values(status="running", locked_by=worker_id, locked_at=moment,
             heartbeat_at=moment, attempts=models.Job.attempts + 1,
             updated_at=moment).returning(models.Job.id))
    if job_id is None:
        db.commit()
        return None
    row = db.get(models.Job, job_id, populate_existing=True)
    result = ClaimedJob(row.id, row.kind, row.session_id, dict(row.payload))
    db.commit()
    return result


def recover(db, worker_id: str, *, startup: bool = False, now: datetime | None = None) -> int:
    moment = now or now_utc()
    condition = or_(models.Job.locked_by != worker_id, models.Job.locked_by.is_(None)) if startup else or_(
        models.Job.heartbeat_at < moment - timedelta(minutes=5),
        models.Job.heartbeat_at.is_(None))
    rows = db.scalars(select(models.Job).where(models.Job.status == "running", condition)).all()
    for row in rows:
        row.status = ("failed" if row.max_attempts is not None and row.attempts >= row.max_attempts else "queued")
        row.locked_by = row.locked_at = row.heartbeat_at = None
        row.run_after = row.updated_at = moment
        row.last_error = "worker lease recovered"
        if row.status == "failed" and row.kind == "transcode":
            db.execute(update(models.Recording).where(
                models.Recording.session_id == row.session_id,
                models.Recording.recording_state == "transcoding",
            ).values(recording_state="transcode_failed", updated_at=moment))
    db.commit()
    return len(rows)


def reconcile(db) -> None:
    """Persist cleanup; the worker performs IO later, outside this transaction."""
    for rec in db.scalars(select(models.Recording).where(models.Recording.recording_state.in_(TOMBSTONES))):
        enqueue(db, "delete_recording", rec.session_id)
    db.commit()


class JobCancelled(Exception):
    pass


def _sqlite_busy(exc: BaseException) -> bool:
    """True when SQLite refused a short read because another writer holds the file."""
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        text = str(current).lower()
        if "database is locked" in text or "database is busy" in text:
            return True
        pending.append(current.__cause__)
        pending.append(current.__context__)
        pending.append(getattr(current, "orig", None))
    return False


class Worker:
    def __init__(self, *, store: storage.Storage | None = None):
        self.worker_id = str(uuid.uuid4())
        self.store = store or storage.get_storage()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._last_maintenance = self._last_reconcile = float("-inf")

    def start(self) -> None:
        with models.session_scope() as db:
            recover(db, self.worker_id, startup=True)
            reconcile(db)
        self.thread = threading.Thread(target=self._loop, name="recording-jobs", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)
        if self.thread and self.thread.is_alive():
            logger.warning("job IO still stopping; next startup will recover its lease")

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.maintenance()
                if self.run_once():
                    continue
            except Exception:
                logger.exception("job queue iteration failed")
            self.stop_event.wait(0.5)

    def maintenance(self) -> None:
        from . import auth, recording_integrity
        tick = time.monotonic()
        if tick - self._last_maintenance < 60:
            return
        self._last_maintenance = tick
        with models.session_scope() as db:
            recover(db, self.worker_id)
            for session in db.scalars(select(models.Session).where(models.Session.status == "ended")):
                if session.ended_at is None:
                    continue
                rec = models.get_recording(db, session.id)
                cutoff = recording_integrity.grace_synthesize_cutoff()
                if rec is None and (cutoff is None or naive(session.ended_at) < naive(cutoff)):
                    continue
                if rec is None or rec.recording_state in {"recording", "finalizing"}:
                    enqueue(db, "finalize_timeout", session.id,
                            run_after=session.ended_at + timedelta(seconds=auth.upload_grace_seconds()))
            for rec in db.scalars(select(models.Recording).where(
                models.Recording.recording_state.in_(("complete", "transcoded")),
                models.Recording.finalize_state == "complete",
                models.Recording.needs_operator.is_(False),
            )):
                enqueue(db, "upload" if rec.recording_state == "transcoded" else "transcode", rec.session_id)
            day = now_utc().date().isoformat()
            existing = db.scalar(select(models.Job.id).where(
                models.Job.kind == "sqlite_backup", models.Job.session_id == day))
            if existing is None:
                enqueue(db, "sqlite_backup", day)
            db.commit()
            if tick - self._last_reconcile >= 3600:
                reconcile(db)
                self._last_reconcile = tick

    def _require_runnable(self, job: ClaimedJob, *, status: str | None, locked_by: str | None,
                          have_recording: bool, recording_state: str | None, needs_operator: bool,
                          finalize_state: str | None, incomplete_reason: str | None) -> None:
        if status != "running" or locked_by != self.worker_id:
            raise JobCancelled("job cancelled")
        if job.kind in DELETE_KINDS | {"sqlite_backup", "finalize_timeout"}:
            return
        if (not have_recording or recording_state in TOMBSTONES or needs_operator
                or finalize_state != "complete" or incomplete_reason):
            raise JobCancelled("recording is not complete")

    def _guard(self, job: ClaimedJob) -> dict:
        if self.stop_event.is_set():
            raise JobCancelled("worker stopping")
        with models.session_scope() as db:
            # Read and release. A shared lock must not stay open across FFmpeg.
            row = db.get(models.Job, job.id)
            status = None if row is None else row.status
            locked_by = None if row is None else row.locked_by
            if job.kind in DELETE_KINDS | {"sqlite_backup", "finalize_timeout"}:
                self._require_runnable(job, status=status, locked_by=locked_by, have_recording=False,
                                       recording_state=None, needs_operator=False, finalize_state=None,
                                       incomplete_reason=None)
                db.rollback()
                return {}
            rec = models.get_recording(db, job.session_id)
            if rec is None:
                state = key = manifest = final_sequence = total_chunks = total_duration_ms = None
                needs_operator, finalize, incomplete, have_recording = False, None, None, False
            else:
                state = rec.recording_state
                needs_operator = bool(rec.needs_operator)
                finalize = rec.finalize_state
                incomplete = rec.incomplete_reason
                key = rec.recording_key
                manifest = dict(rec.manifest) if isinstance(rec.manifest, dict) else rec.manifest
                final_sequence = rec.final_sequence
                total_chunks = rec.total_chunks
                total_duration_ms = rec.total_duration_ms
                have_recording = True
            self._require_runnable(job, status=status, locked_by=locked_by, have_recording=have_recording,
                                   recording_state=state, needs_operator=needs_operator,
                                   finalize_state=finalize, incomplete_reason=incomplete)
            db.rollback()
        return {"recording_state": state, "recording_key": key, "manifest": manifest,
                "final_sequence": final_sequence, "total_chunks": total_chunks,
                "total_duration_ms": total_duration_ms}

    def _ffmpeg_cancel_check(self, job: ClaimedJob) -> None:
        """Notice delete/shutdown while FFmpeg runs, without aborting on a busy database.

        The poll used to borrow a pooled session. Under load that read raises
        ``database is locked``; the remux loop treated every exception as fatal,
        killed FFmpeg, and left ``student.part.mp4`` behind. A dedicated
        connection with a short busy timeout either observes a committed cancel
        or skips this tick. The next poll retries. The write lock is never held
        here, and the read lock is dropped before the sleep.
        """
        if self.stop_event.is_set():
            raise JobCancelled("worker stopping")
        url = models.engine.url
        database = url.database if url.get_backend_name() == "sqlite" else None
        if not database or database == ":memory:":
            self._guard(job)
            return
        conn = sqlite3.connect(database, timeout=0.2)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            job_row = conn.execute(
                "SELECT status, locked_by FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            rec = conn.execute(
                "SELECT recording_state, needs_operator, finalize_state, incomplete_reason "
                "FROM recordings WHERE session_id = ?",
                (job.session_id,),
            ).fetchone()
            conn.rollback()
        except sqlite3.OperationalError as exc:
            if _sqlite_busy(exc):
                logger.warning("job %s transcode cancel check skipped; database is locked", job.id)
                return
            raise
        finally:
            conn.close()
        self._require_runnable(
            job,
            status=None if job_row is None else job_row["status"],
            locked_by=None if job_row is None else job_row["locked_by"],
            have_recording=rec is not None,
            recording_state=None if rec is None else rec["recording_state"],
            needs_operator=bool(rec["needs_operator"]) if rec is not None else False,
            finalize_state=None if rec is None else rec["finalize_state"],
            incomplete_reason=None if rec is None else rec["incomplete_reason"],
        )

    def _heartbeat(self, job: ClaimedJob, done: threading.Event) -> None:
        # This helper only renews a lease; it never executes a second job.
        while not done.wait(30):
            try:
                with models.session_scope() as db:
                    db.execute(update(models.Job).where(
                        models.Job.id == job.id, models.Job.status == "running",
                        models.Job.locked_by == self.worker_id,
                    ).values(heartbeat_at=now_utc()))
                    db.commit()
            except Exception:
                logger.warning("could not renew job %s lease", job.id)

    def run_once(self) -> bool:
        with models.session_scope() as db:
            job = claim(db, self.worker_id)
        if job is None:
            return False
        done = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(job, done), daemon=True, name="job-lease")
        heartbeat.start()
        try:
            self._guard(job)
            getattr(self, "_" + job.kind)(job)
        except JobCancelled:
            self._finish(job, cancelled=True)
        except Exception as exc:
            logger.warning("job %s (%s) failed: %s", job.id, job.kind, type(exc).__name__)
            self._finish(job, error=type(exc).__name__)
        else:
            self._finish(job)
        finally:
            done.set()
            heartbeat.join(timeout=1)
        return True

    def _finish(self, job: ClaimedJob, *, error: str | None = None, cancelled: bool = False) -> None:
        # A lock at the commit must not leave a finished or cancelled job `running`.
        # The status guard makes a retry after a committed attempt a no-op.
        delay = 0.05
        for attempt in range(6):
            try:
                self._finish_once(job, error=error, cancelled=cancelled)
                return
            except Exception as exc:
                if attempt == 5 or not _sqlite_busy(exc):
                    raise
                logger.warning("job %s finish waiting on sqlite lock", job.id)
                time.sleep(delay)
                delay = min(delay * 2, 0.4)

    def _finish_once(self, job: ClaimedJob, *, error: str | None = None, cancelled: bool = False) -> None:
        with models.session_scope() as db:
            row = db.get(models.Job, job.id)
            if row is None or row.status != "running" or row.locked_by != self.worker_id:
                return
            if cancelled and self.stop_event.is_set():
                row.status = "queued"  # Graceful stop is not an exhausted retry.
                row.attempts = max(0, row.attempts - 1)
            elif cancelled:
                row.status = "cancelled"
            elif error:
                row.last_error = error
                row.status = "failed" if row.max_attempts is not None and row.attempts >= row.max_attempts else "queued"
                row.run_after = now_utc() + timedelta(seconds=BACKOFF[min(row.attempts - 1, 2)])
                if job.kind == "transcode":
                    db.execute(update(models.Recording).where(
                        models.Recording.session_id == job.session_id,
                        ~models.Recording.recording_state.in_(TOMBSTONES),
                        models.Recording.finalize_state == "complete",
                    ).values(recording_state="transcode_failed"))
            else:
                row.status, row.last_error = "succeeded", None
            row.locked_by = row.locked_at = row.heartbeat_at = None
            row.updated_at = now_utc()
            db.commit()

    def _set_recording(self, job: ClaimedJob, **values) -> None:
        self._guard(job)
        with models.session_scope() as db:
            changed = db.execute(update(models.Recording).where(
                models.Recording.session_id == job.session_id,
                ~models.Recording.recording_state.in_(TOMBSTONES),
                models.Recording.needs_operator.is_(False),
                models.Recording.finalize_state == "complete",
            ).values(updated_at=now_utc(), **values)).rowcount
            db.commit()
            if not changed:
                raise JobCancelled()

    def _valid_mp4(self, path, expected_ms: int | None = None) -> bool:
        from . import recording
        if not path.is_file() or not path.stat().st_size:
            return False
        probe = recording.probe_media(path)
        if (not probe or not probe["has_video"] or not probe.get("duration_s")
                or not math.isfinite(probe["duration_s"]) or probe["duration_s"] <= 0
                or "h264" not in probe.get("codecs", [])
                or (probe.get("has_audio") and "aac" not in probe.get("codecs", []))):
            return False
        if expected_ms is not None and abs(probe["duration_s"] - expected_ms / 1000) >= 1:
            return False
        return True

    def _transcode(self, job: ClaimedJob) -> None:
        from . import recording
        facts = self._guard(job)
        if not facts["manifest"]:
            sequences = [seq for seq, _ in recording.stored_chunks(job.session_id)]
            final = facts["final_sequence"]
            if (final is None or facts["total_chunks"] != final + 1
                    or sequences != list(range(final + 1))
                    or not recording.validate_header_chunk(job.session_id)):
                raise RuntimeError("source completeness changed; original media retained")
        self._set_recording(job, recording_state="transcoding")
        mp4 = recording.mp4_path(job.session_id)
        if not self._valid_mp4(mp4, facts["total_duration_ms"]):
            result = recording.process_recording(
                job.session_id, cancel_check=lambda: self._ffmpeg_cancel_check(job)
            )
            if result["status"] != "completed" or not self._valid_mp4(mp4, facts["total_duration_ms"]):
                raise RuntimeError("MP4 validation failed; original media retained")
            if result.get("duration_delta_s") is not None and result["duration_delta_s"] >= 1:
                raise RuntimeError("MP4 duration mismatch; original media retained")
        self._guard(job)
        manifest = facts["manifest"] or {
            "final_sequence": facts["final_sequence"], "missing_sequences": [],
            "chunks": [{"sequence": seq, "sha256": storage.file_sha256(path), "size_bytes": path.stat().st_size}
                       for seq, path in recording.stored_chunks(job.session_id)],
        }
        self._set_recording(job, recording_state="transcoded", manifest=manifest,
                            recording_sha256=storage.file_sha256(mp4))
        self._guard(job)
        # Manifest and validated MP4 are durable before these disposable originals.
        chunks = recording.chunk_dir(job.session_id)
        if chunks.exists():
            shutil.rmtree(chunks)
        recording.merged_path(job.session_id).unlink(missing_ok=True)
        with models.session_scope() as db:
            rec = models.get_recording(db, job.session_id)
            if rec and rec.recording_state not in TOMBSTONES:
                enqueue(db, "upload", job.session_id)
                db.commit()

    def _upload(self, job: ClaimedJob) -> None:
        from . import recording
        facts = self._guard(job)
        path = recording.mp4_path(job.session_id)
        if not self._valid_mp4(path, facts["total_duration_ms"]):
            raise RuntimeError("verified local MP4 required")
        checksum, key = storage.file_sha256(path), storage.recording_key(job.session_id)
        self._guard(job)
        if not self.store.exists(key, checksum):
            self._guard(job)
            self.store.put_file(key, path, checksum)
        with models.session_scope() as db:
            changed = db.execute(update(models.Recording).where(
                models.Recording.session_id == job.session_id,
                ~models.Recording.recording_state.in_(TOMBSTONES),
                models.Recording.needs_operator.is_(False),
                models.Recording.finalize_state == "complete",
            ).values(recording_state="uploaded", recording_key=key,
                     recording_sha256=checksum, updated_at=now_utc())).rowcount
            if not changed:
                enqueue(db, "delete_orphan_object", job.session_id, payload={"key": key})
            db.commit()
        if not changed:
            raise JobCancelled("recording deleted during upload")

    def _delete_recording(self, job: ClaimedJob) -> None:
        from . import recording
        with models.session_scope() as db:
            rec = models.get_recording(db, job.session_id)
            if rec is None or rec.recording_state not in TOMBSTONES:
                return
            keys = {storage.recording_key(job.session_id)}
            if rec.recording_key:
                keys.add(rec.recording_key)
        for key in keys:
            self._guard(job)
            self.store.delete(key)
        self._guard(job)
        directory = recording.session_dir(job.session_id)
        if directory.exists():
            shutil.rmtree(directory)
        with models.session_scope() as db:
            rec = models.get_recording(db, job.session_id)
            if rec and rec.recording_state == "deleting":
                rec.recording_state, rec.recording_key = "deleted", None
                rec.deleted_at, rec.updated_at = now_utc(), now_utc()
                models.record_audit(db, action="recording_deleted", actor_role="server",
                                    session_id=job.session_id, details={}, commit=False)
            db.commit()

    def _delete_orphan_object(self, job: ClaimedJob) -> None:
        # Never allow an arbitrary payload to delete a different session's object.
        key = storage.recording_key(job.session_id)
        if job.payload.get("key", key) != key:
            raise ValueError("orphan key does not match session")
        self.store.delete(key)

    def _finalize_timeout(self, job: ClaimedJob) -> None:
        from . import recording_integrity
        with models.session_scope() as db:
            recording_integrity.apply_upload_grace_deadlines(db, session_id=job.session_id)

    def _sqlite_backup(self, job: ClaimedJob) -> None:
        from . import recording
        directory = recording.SESSIONS_DIR.resolve() / "_backups"
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f"{job.id}.db.part"
        archive = directory / f"{job.id}.db.gz"
        raw = models.engine.raw_connection()
        try:
            dest = sqlite3.connect(temp)
            try:
                raw.driver_connection.backup(dest)
            finally:
                dest.close()
        finally:
            raw.close()
        try:
            compressed = archive.with_name(archive.name + ".part")
            with temp.open("rb") as source, gzip.open(compressed, "wb") as output:
                shutil.copyfileobj(source, output)
            compressed.replace(archive)
            self._guard(job)
            key = f"backups/sqlite/{job.session_id}/mindprobe.db.gz"
            self.store.put_file(key, archive, storage.file_sha256(archive))
        finally:
            temp.unlink(missing_ok=True)
