"""ADR-0005 durable queue, destructive-work fencing and storage acceptance."""
from __future__ import annotations

import gzip
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app import jobs, models, recording, recording_integrity, storage
from app.main import app

OP = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(models, "engine", engine)
    monkeypatch.setattr(models, "SessionLocal", factory)
    monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setenv("MINDPROBE_LOCAL_STORAGE_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("MINDPROBE_STORAGE_BACKEND", "local")
    models.init_db()
    def dependency():
        with factory() as db:
            yield db
    app.dependency_overrides[models.get_db] = dependency
    yield factory, TestClient(app), storage.get_storage()
    app.dependency_overrides.pop(models.get_db, None)
    engine.dispose()


def take(factory, sid="TAKE", *, state="complete"):
    with factory() as db:
        models.create_session(db, sid, "CASE001")
        rec = models.ensure_recording(db, sid)
        rec.recording_state = state
        rec.finalize_state = "complete" if state != "incomplete" else "incomplete"
        rec.final_sequence, rec.total_chunks, rec.total_duration_ms = 0, 1, 2000
        if state == "incomplete":
            rec.incomplete_reason, rec.needs_operator = "recorder_lost", True
        db.commit()
    path = recording.chunk_path(sid, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(recording.EBML_MAGIC + b"test")


def queued(factory, kind, sid="TAKE", **kwargs):
    with factory() as db:
        job_id = jobs.enqueue(db, kind, sid, **kwargs)
        db.commit()
        return job_id


def job_row(factory, job_id):
    with factory() as db:
        return db.get(models.Job, job_id)


def fake_probe(monkeypatch):
    monkeypatch.setattr(recording, "probe_media", lambda path: {
        "has_video": True, "has_audio": True, "duration_s": 2.0, "codecs": ["h264", "aac"],
    })


def test_enqueue_is_transactional_and_active_unique(env):
    factory, _, _ = env
    take(factory)
    with factory() as db:
        first = jobs.enqueue(db, "transcode", "TAKE")
        assert jobs.enqueue(db, "transcode", "TAKE") == first
        db.rollback()
    with factory() as db:
        assert db.scalar(select(models.Job.id)) is None
    first = queued(factory, "transcode")
    with factory() as db:
        db.get(models.Job, first).status = "succeeded"
        db.commit()
    assert queued(factory, "transcode") != first


def test_concurrent_claim_executes_exactly_once(env):
    factory, _, _ = env
    job_id = queued(factory, "sqlite_backup", "2026-10-26")
    barrier = threading.Barrier(2)
    def claim(wid):
        barrier.wait()
        with factory() as db:
            row = jobs.claim(db, wid)
            return row.id if row else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(claim, ("one", "two")))
    assert sorted(x for x in result if x is not None) == [job_id]
    assert job_row(factory, job_id).attempts == 1


def test_restart_reclaims_old_instance_immediately_and_respects_attempt_limit(env):
    factory, _, _ = env
    jid = queued(factory, "transcode")
    with factory() as db:
        jobs.claim(db, "dead")
        assert jobs.recover(db, "new", startup=True) == 1
        assert db.get(models.Job, jid).status == "queued"
        jobs.claim(db, "dead")
        row = db.get(models.Job, jid)
        row.attempts = 3
        db.commit()
        jobs.recover(db, "new", startup=True)
    assert job_row(factory, jid).status == "failed"


def test_stale_lease_only_recovered_after_five_minutes(env):
    factory, _, _ = env
    jid = queued(factory, "upload")
    now = jobs.now_utc()
    with factory() as db:
        jobs.claim(db, "old", now=now)
        assert jobs.recover(db, "new", now=now + timedelta(minutes=4)) == 0
        assert jobs.recover(db, "new", now=now + timedelta(minutes=6)) == 1
    assert job_row(factory, jid).status == "queued"


@pytest.mark.parametrize("kind,limit", [("upload", 3), ("delete_recording", None)])
def test_failure_retry_bounds_and_delete_never_abandoned(env, monkeypatch, kind, limit):
    factory, _, store = env
    take(factory)
    jid = queued(factory, kind)
    worker = jobs.Worker(store=store)
    def fail(_): raise OSError("simulated")
    monkeypatch.setattr(worker, "_" + kind, fail)
    for attempt in range(1, 5 if limit is None else 4):
        with factory() as db:
            row = db.get(models.Job, jid)
            row.run_after = jobs.now_utc() - timedelta(seconds=1)
            db.commit()
        assert worker.run_once()
        row = job_row(factory, jid)
        assert row.attempts == attempt
        if attempt < 3 or limit is None:
            assert row.status == "queued"
            assert row.run_after > jobs.now_utc()
        else:
            assert row.status == "failed"
    assert row.max_attempts == limit


def test_recording_status_410_and_auth_json_are_not_stored(env):
    """Chromium caches a 410 with no Cache-Control and never sees `deleted`."""
    factory, client, _ = env
    take(factory)
    session = client.get("/sessions/TAKE", headers=OP)
    assert session.status_code == 200
    assert session.headers["cache-control"] == "no-store"
    live = client.get("/sessions/TAKE/recording/status", headers=OP)
    assert live.status_code == 200
    assert live.headers["cache-control"] == "no-store"
    assert client.request(
        "DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"}
    ).status_code == 202
    gone = client.get("/sessions/TAKE/recording/status", headers=OP)
    assert gone.status_code == 410
    assert gone.json()["detail"]["recording_state"] == "deleting"
    assert gone.headers["cache-control"] == "no-store"
    missing = client.get("/sessions/TAKE/recording/status")
    assert missing.status_code == 401
    assert missing.headers["cache-control"] == "no-store"


def test_delete_short_transaction_idempotency_and_tombstones(env):
    factory, client, store = env
    take(factory)
    jid = queued(factory, "transcode")
    with factory() as db:
        jobs.claim(db, "another-worker")
    response = client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "wrong"})
    assert response.status_code == 400
    assert client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"}).status_code == 202
    assert recording.chunk_path("TAKE", 0).exists()  # no filesystem IO in DELETE
    assert job_row(factory, jid).status == "cancelled"
    assert client.get("/sessions/TAKE/recording", headers=OP).status_code == 410
    assert client.get("/sessions/TAKE/recording/status", headers=OP).status_code == 410
    assert client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"}).status_code == 202
    worker = jobs.Worker(store=store)
    assert worker.run_once()
    assert not recording.session_dir("TAKE").exists()
    with factory() as db:
        rec = models.get_recording(db, "TAKE")
        assert rec.recording_state == "deleted" and rec.recording_key is None and rec.deleted_at
        assert db.scalar(select(models.Session.id)) == "TAKE"
        assert len(db.scalars(select(models.AuditLog).where(models.AuditLog.action == "recording_delete_requested")).all()) == 1
        assert len(db.scalars(select(models.AuditLog).where(models.AuditLog.action == "recording_deleted")).all()) == 1


@pytest.mark.parametrize("role,typ,status", [
    ("teacher", "live", 401), ("observer", "review", 403),
    ("student", "live", 401), ("student", "upload", 401),
])
def test_delete_rejects_wrong_role_and_type(env, role, typ, status):
    from app import websocket
    factory, client, _ = env
    take(factory)
    if typ == "review":
        codes = client.post("/sessions/TAKE/review-codes", headers=OP).json()["review_codes"]
        token = client.post("/sessions/TAKE/review-login", json={"review_code": codes[role]}).json()["access_token"]
    else:
        token = websocket.create_session_token(session_id="TAKE", role=role, client_id="qa", typ=typ)
    response = client.request("DELETE", "/sessions/TAKE/recording",
                              headers={"Authorization": f"Bearer {token}"}, json={"confirm": "TAKE"})
    assert response.status_code == status


def test_teacher_review_delete_strict_after_logout_and_revoke(env):
    factory, client, _ = env
    take(factory)
    codes = client.post("/sessions/TAKE/review-codes", headers=OP).json()["review_codes"]
    token = client.post("/sessions/TAKE/review-login", json={"review_code": codes["teacher"]}).json()["access_token"]
    header = {"Authorization": f"Bearer {token}"}
    assert client.post("/sessions/TAKE/end", headers=OP).status_code == 200
    assert client.request("DELETE", "/sessions/TAKE/recording", headers=header, json={"confirm": "TAKE"}).status_code == 202
    assert client.post("/sessions/TAKE/review-logout", headers=header).status_code == 200
    assert client.request("DELETE", "/sessions/TAKE/recording", headers=header, json={"confirm": "TAKE"}).status_code == 401


def test_anonymous_and_cookie_delete_are_refused(env):
    factory, client, _ = env
    take(factory)
    assert client.request("DELETE", "/sessions/TAKE/recording", headers={"X-No-Operator": "1"}, json={"confirm": "TAKE"}).status_code == 401
    assert client.request("DELETE", "/sessions/TAKE/recording", headers={"Cookie": "mindprobe_session_token=x"}, json={"confirm": "TAKE"}).status_code == 401


def test_upload_finishing_after_delete_queues_durable_orphan_cleanup(env, monkeypatch):
    factory, client, store = env
    take(factory, state="transcoded")
    recording.mp4_path("TAKE").write_bytes(b"verified-mp4")
    fake_probe(monkeypatch)
    original = store.put_file
    def racing_put(key, path, checksum):
        original(key, path, checksum)
        assert client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"}).status_code == 202
    monkeypatch.setattr(store, "put_file", racing_put)
    jid = queued(factory, "upload")
    worker = jobs.Worker(store=store)
    assert worker.run_once()
    assert job_row(factory, jid).status == "cancelled"
    with factory() as db:
        assert db.scalar(select(models.Job.id).where(models.Job.kind == "delete_orphan_object")) is not None
    assert store.exists(storage.recording_key("TAKE"))
    while worker.run_once(): pass
    assert not store.exists(storage.recording_key("TAKE"))


def test_restart_reconciles_already_deleted_null_key_and_cancelled_upload(env):
    factory, _, store = env
    take(factory, state="deleted")
    path = recording.chunk_path("TAKE", 0)
    key = storage.recording_key("TAKE")
    store.put_file(key, path, storage.file_sha256(path))  # uploaded immediately before a crash
    jid = queued(factory, "upload")
    with factory() as db:
        db.get(models.Job, jid).status = "cancelled"
        db.commit()
        jobs.recover(db, "new", startup=True)
        jobs.reconcile(db)
    worker = jobs.Worker(store=store)
    assert worker.run_once()
    assert not store.exists(key)
    assert not recording.session_dir("TAKE").exists()


def test_failed_delete_keeps_tombstone_and_raw_then_retries(env, monkeypatch):
    factory, client, store = env
    take(factory)
    client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"})
    original = store.delete
    def fail(key): raise OSError("offline storage")
    monkeypatch.setattr(store, "delete", fail)
    worker = jobs.Worker(store=store)
    worker.run_once()
    assert recording.chunk_path("TAKE", 0).exists()
    with factory() as db:
        assert models.get_recording(db, "TAKE").recording_state == "deleting"
        row = db.scalar(select(models.Job).where(models.Job.kind == "delete_recording"))
        assert row.status == "queued" and row.max_attempts is None
        row.run_after = jobs.now_utc()
        db.commit()
    monkeypatch.setattr(store, "delete", original)
    worker.run_once()
    assert not recording.session_dir("TAKE").exists()


def test_transcode_manifest_precedes_cleanup_and_status_survives(env, monkeypatch):
    factory, _, store = env
    take(factory)
    fake_probe(monkeypatch)
    def process(sid, **kwargs):
        recording.mp4_path(sid).write_bytes(b"verified-mp4")
        return {"status": "completed"}
    monkeypatch.setattr(recording, "process_recording", process)
    queued(factory, "transcode")
    worker = jobs.Worker(store=store)
    assert worker.run_once()
    assert not recording.chunk_dir("TAKE").exists()
    assert recording.mp4_path("TAKE").exists()
    with factory() as db:
        rec = models.get_recording(db, "TAKE")
        assert rec.manifest["chunks"][0]["sha256"]
        assert recording_integrity.student_upload_status("TAKE", db)["received_sequences"] == [0]
    assert worker.run_once()
    assert store.exists(storage.recording_key("TAKE"))
    with factory() as db:
        assert models.get_recording(db, "TAKE").recording_state == "uploaded"


@pytest.mark.parametrize("state", ["incomplete", "complete"])
def test_incomplete_or_failed_validation_never_cleans_raw(env, monkeypatch, state):
    factory, _, store = env
    take(factory, state=state)
    monkeypatch.setattr(recording, "process_recording", lambda *args, **kwargs: {"status": "raw_only"})
    queued(factory, "transcode")
    jobs.Worker(store=store).run_once()
    assert recording.chunk_path("TAKE", 0).exists()
    with factory() as db:
        assert models.get_recording(db, "TAKE").recording_state == ("incomplete" if state == "incomplete" else "transcode_failed")


def test_worker_rechecks_source_completeness_before_reusing_mp4(env, monkeypatch):
    factory, _, store = env
    take(factory)
    recording.mp4_path("TAKE").write_bytes(b"apparently-valid-mp4")
    fake_probe(monkeypatch)
    with factory() as db:
        rec = models.get_recording(db, "TAKE")
        rec.final_sequence, rec.total_chunks = 1, 2  # the declared tail is gone
        db.commit()
    jid = queued(factory, "transcode")
    jobs.Worker(store=store).run_once()
    assert job_row(factory, jid).status == "queued"
    assert recording.chunk_path("TAKE", 0).exists()
    assert not store.exists(storage.recording_key("TAKE"))


def test_playback_polling_does_not_reset_exhausted_retry_budget(env):
    factory, client, _ = env
    take(factory)
    jid = queued(factory, "transcode")
    with factory() as db:
        row = db.get(models.Job, jid)
        row.status, row.attempts = "failed", 3
        db.commit()
    for _ in range(2):
        response = client.get("/sessions/TAKE/recording", headers=OP)
        assert response.status_code == 409 and response.json()["detail"] == "processing_failed"
    with factory() as db:
        assert len(db.scalars(select(models.Job)).all()) == 1
    assert client.post("/recording/TAKE/process", headers=OP).status_code == 202


def test_signed_local_playback_range_expiry_and_tombstone(env, monkeypatch):
    monkeypatch.delenv("MINDPROBE_PRESIGN_TTL_SECONDS", raising=False)
    factory, client, store = env
    take(factory, state="uploaded")
    key = storage.recording_key("TAKE")
    path = recording.chunk_path("TAKE", 0)
    store.put_file(key, path, storage.file_sha256(path))
    with factory() as db:
        models.get_recording(db, "TAKE").recording_key = key
        db.commit()
    response = client.get("/sessions/TAKE/recording", headers=OP)
    assert response.status_code == 200
    url = response.json()["url"]
    assert response.json()["expires_in"] == 900
    assert client.get(url, headers={"Range": "bytes=0-3"}).content == recording.EBML_MAGIC
    assert client.get(url, headers={"Range": "bytes=0-3"}).status_code == 206
    assert client.get(url.replace("/TAKE?", "/OTHER?")).status_code == 401
    assert client.get(url + "x").status_code == 401
    original = storage.time.time
    with monkeypatch.context() as clock:
        clock.setattr(storage.time, "time", lambda: original() + 901)
        assert client.get(url).status_code == 401
    client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"})
    assert client.get(url).status_code == 410


def test_heartbeat_renews_lease_without_executing_another_job(env):
    factory, _, store = env
    old = jobs.now_utc() - timedelta(minutes=6)
    jid = queued(factory, "sqlite_backup", "2026-10-26", run_after=old)
    worker = jobs.Worker(store=store)
    with factory() as db:
        claimed = jobs.claim(db, worker.worker_id, now=old)
    class Once:
        calls = 0
        def wait(self, seconds):
            assert seconds == 30
            self.calls += 1
            return self.calls > 1
    worker._heartbeat(claimed, Once())
    assert job_row(factory, jid).heartbeat_at > old
    with factory() as db:
        assert jobs.recover(db, "other") == 0


def test_startup_enqueues_reconciliation_and_recovers_lease(env, monkeypatch):
    factory, _, store = env
    take(factory, state="deleted")
    jid = queued(factory, "upload")
    with factory() as db:
        jobs.claim(db, "previous-process")
    worker = jobs.Worker(store=store)
    monkeypatch.setattr(worker, "_loop", lambda: None)
    worker.start()
    worker.stop()
    assert job_row(factory, jid).status == "queued"
    with factory() as db:
        assert db.scalar(select(models.Job.id).where(models.Job.kind == "delete_recording"))


def test_hourly_reconciliation_revisits_deleted_fixed_key(env, monkeypatch):
    factory, _, store = env
    take(factory, state="deleted")
    moment = [0.0]
    monkeypatch.setattr(jobs.time, "monotonic", lambda: moment[0])
    worker = jobs.Worker(store=store)
    worker.maintenance()
    with factory() as db:
        row = db.scalar(select(models.Job).where(models.Job.kind == "delete_recording"))
        row.status = "succeeded"
        db.commit()
    moment[0] = 61
    worker.maintenance()
    with factory() as db:
        assert len(db.scalars(select(models.Job).where(models.Job.kind == "delete_recording")).all()) == 1
    moment[0] = 3601
    worker.maintenance()
    with factory() as db:
        assert len(db.scalars(select(models.Job).where(models.Job.kind == "delete_recording")).all()) == 2


def test_delete_cancels_running_ffmpeg_before_output_publication(env, monkeypatch):
    factory, client, store = env
    take(factory)
    fake_probe(monkeypatch)
    part = recording.mp4_path("TAKE").with_name("student.part.mp4")
    class Process:
        killed = waited = False
        returncode = None
        def __init__(self, *args, **kwargs):
            part.write_bytes(b"partial-transcode")
            assert client.request("DELETE", "/sessions/TAKE/recording", headers=OP,
                                  json={"confirm": "TAKE"}).status_code == 202
        def poll(self): return self.returncode
        def kill(self):
            Process.killed = True
            self.returncode = -9
        def wait(self): Process.waited = True
    monkeypatch.setattr(recording.subprocess, "Popen", Process)
    jid = queued(factory, "transcode")
    worker = jobs.Worker(store=store)
    assert worker.run_once()
    assert Process.killed and Process.waited
    assert job_row(factory, jid).status == "cancelled"
    assert recording.chunk_path("TAKE", 0).exists()
    assert not part.exists()
    assert not recording.mp4_path("TAKE").exists()
    # A second observation is still a cancel, and it does not recreate the partial.
    with pytest.raises(jobs.JobCancelled):
        worker._ffmpeg_cancel_check(jobs.ClaimedJob(jid, "transcode", "TAKE", {}))
    assert not part.exists()
    assert client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"}).status_code == 202
    assert worker.run_once()
    assert not recording.session_dir("TAKE").exists()


def test_sqlite_lock_on_cancel_poll_does_not_abort_transcode(env, monkeypatch, caplog):
    """A busy database during the 0.2s poll must not kill FFmpeg or leave a part file."""
    factory, _, store = env
    take(factory)
    fake_probe(monkeypatch)
    monkeypatch.setattr(recording.time, "sleep", lambda _seconds: None)
    part = recording.mp4_path("TAKE").with_name("student.part.mp4")
    db_path = models.engine.url.database
    held = {"conn": None}
    polls = {"n": 0}

    class Process:
        killed = False
        returncode = None
        def __init__(self, *args, **kwargs):
            part.write_bytes(b"partial-transcode")
            # Drop idle pooled transactions so this write lock is the only one.
            models.engine.dispose()
            conn = sqlite3.connect(db_path, timeout=2, isolation_level=None)
            # IMMEDIATE still lets readers through. EXCLUSIVE is the commit-window
            # lock that makes the cancel poll raise "database is locked".
            conn.execute("BEGIN EXCLUSIVE")
            held["conn"] = conn
        def poll(self):
            polls["n"] += 1
            if polls["n"] >= 2 and held["conn"] is not None:
                held["conn"].rollback()
                held["conn"].close()
                held["conn"] = None
                part.write_bytes(b"verified-mp4")
                self.returncode = 0
            return self.returncode
        def kill(self):
            Process.killed = True
            self.returncode = -9
        def wait(self):
            pass

    monkeypatch.setattr(recording.subprocess, "Popen", Process)
    jid = queued(factory, "transcode")
    worker = jobs.Worker(store=store)
    caplog.set_level("WARNING")
    try:
        assert worker.run_once()
    finally:
        if held["conn"] is not None:
            held["conn"].close()
    assert not Process.killed
    assert "database is locked" in caplog.text
    assert job_row(factory, jid).status == "succeeded"
    assert not part.exists()
    assert recording.mp4_path("TAKE").is_file()
    with factory() as db:
        assert models.get_recording(db, "TAKE").recording_state == "transcoded"


def test_finish_retries_sqlite_lock_without_leaving_job_running(env, monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy.exc import OperationalError
    factory, _, store = env
    jid = queued(factory, "sqlite_backup", "2026-10-26")
    worker = jobs.Worker(store=store)
    with factory() as db:
        claimed = jobs.claim(db, worker.worker_id)
    original = models.session_scope
    calls = {"n": 0}

    @contextmanager
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
        with original() as db:
            yield db

    monkeypatch.setattr(models, "session_scope", flaky)
    worker._finish(claimed)
    assert calls["n"] >= 2
    row = job_row(factory, jid)
    assert row.status == "succeeded"
    assert row.locked_by is None


def test_finalization_autoqueues_and_timeout_preserves_missing_take(env):
    factory, client, store = env
    from app import websocket
    take(factory)
    with factory() as db:
        rec = models.get_recording(db, "TAKE")
        rec.recording_state, rec.finalize_state = "recording", "none"
        rec.final_sequence, rec.total_chunks = None, None
        db.commit()
    token = websocket.create_session_token(session_id="TAKE", role="student", client_id="qa")
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/recording/TAKE/finalize", headers=headers,
                           json={"final_sequence": 0, "total_chunks": 1, "total_duration_ms": 2000})
    assert response.status_code == 200 and response.json()["finalize_state"] == "complete"
    with factory() as db:
        assert db.scalar(select(models.Job.id).where(models.Job.kind == "transcode"))
    take(factory, "LATE")
    with factory() as db:
        rec = models.get_recording(db, "LATE")
        rec.recording_state, rec.finalize_state = "finalizing", "none"
        rec.final_sequence, rec.total_chunks = None, None
        session = models.get_session(db, "LATE")
        session.status, session.ended_at = "ended", jobs.now_utc() - timedelta(seconds=601)
        db.commit()
        # Do not run the unrelated queued transcode from TAKE in this test.
        db.execute(update(models.Job).values(status="cancelled"))
        timeout = jobs.enqueue(db, "finalize_timeout", "LATE")
        db.commit()
    jobs.Worker(store=store).run_once()
    assert job_row(factory, timeout).status == "succeeded"
    with factory() as db:
        assert models.get_recording(db, "LATE").incomplete_reason == "grace_expired"
    assert recording.chunk_path("LATE", 0).exists()


def test_process_teacher_review_and_deleted_upload_refusal(env):
    from app import websocket
    factory, client, _ = env
    take(factory)
    codes = client.post("/sessions/TAKE/review-codes", headers=OP).json()["review_codes"]
    token = client.post("/sessions/TAKE/review-login", json={"review_code": codes["teacher"]}).json()["access_token"]
    header = {"Authorization": f"Bearer {token}"}
    assert client.post("/recording/TAKE/process", headers=header).status_code == 202
    assert client.post("/recording/TAKE/process?allow_missing_header=true", headers=header).status_code == 403
    client.request("DELETE", "/sessions/TAKE/recording", headers=OP, json={"confirm": "TAKE"})
    student = websocket.create_session_token(session_id="TAKE", role="student", client_id="qa")
    student_header = {"Authorization": f"Bearer {student}"}
    assert client.get("/recording/TAKE/status", headers=student_header).status_code == 410
    assert client.post("/recording/TAKE/chunk", headers=student_header,
                       data={"sequence": 0}, files={"chunk": ("chunk.webm", b"bytes")}).status_code == 409
    assert client.post("/recording/TAKE/finalize", headers=student_header,
                       json={"final_sequence": 0, "total_chunks": 1, "total_duration_ms": 2000}).status_code == 409
    assert client.post("/recording/TAKE/process", headers=OP).status_code == 409


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None or __import__("shutil").which("ffprobe") is None,
                    reason="real FFmpeg required")
def test_real_ffmpeg_sqlite_lock_during_cancel_poll_leaves_no_part(env, caplog):
    import subprocess
    import time
    factory, _, store = env
    take(factory)
    source = recording.chunk_path("TAKE", 0)
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=10:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "libvpx", "-deadline", "realtime",
                    "-cpu-used", "8", "-c:a", "libopus", str(source)],
                   check=True, capture_output=True)
    part = recording.mp4_path("TAKE").with_name("student.part.mp4")
    db_path = models.engine.url.database
    errors: list[BaseException] = []

    def hold_write_lock():
        try:
            deadline = time.time() + 20
            while not part.exists() and time.time() < deadline:
                time.sleep(0.01)
            if not part.exists():
                raise RuntimeError("transcode never created a partial mp4")
            conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
            try:
                # IMMEDIATE still lets readers through. EXCLUSIVE matches the
                # lock SQLite takes while another connection is committing.
                # sqlite3's timeout is a lower bound. On Windows, timeout=0.2
                # (the cancel poll) does not return for ~0.55s, and timeout=1
                # takes ~1.7s. A 0.7s hold ended before that wait, so the poll
                # succeeded and never logged "database is locked". Hold the
                # lock well past either wait so Linux and Windows both hit it.
                conn.execute("BEGIN EXCLUSIVE")
                time.sleep(3)
                conn.rollback()
            finally:
                conn.close()
        except BaseException as exc:
            errors.append(exc)

    locker = threading.Thread(target=hold_write_lock, daemon=True)
    jid = queued(factory, "transcode")
    worker = jobs.Worker(store=store)
    caplog.set_level("WARNING")
    locker.start()
    try:
        assert worker.run_once()
    finally:
        locker.join(timeout=20)
    assert not errors, errors
    assert "database is locked" in caplog.text
    assert job_row(factory, jid).status == "succeeded"
    assert not part.exists()
    assert recording.mp4_path("TAKE").is_file()
    with factory() as db:
        assert models.get_recording(db, "TAKE").recording_state == "transcoded"


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None or __import__("shutil").which("ffprobe") is None,
                    reason="real FFmpeg required")
def test_real_worker_transcodes_uploads_and_retains_only_validated_mp4(env, tmp_path):
    import subprocess
    import sys
    factory, client, store = env
    take(factory)
    source = recording.chunk_path("TAKE", 0)
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=10:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "libvpx", "-deadline", "realtime",
                    "-cpu-used", "8", "-c:a", "libopus", str(source)],
                   check=True, capture_output=True)
    jid = queued(factory, "transcode")
    # A separate process commits its claim and exits abruptly. Recovery must
    # work from SQLite alone, without waiting for its heartbeat to expire.
    crash = subprocess.run([sys.executable, "-c", """
import os, sys
os.environ['MINDPROBE_DB_URL'] = 'sqlite:///' + sys.argv[1]
from app import jobs, models
with models.session_scope() as db:
    assert jobs.claim(db, 'crashed-worker') is not None
os._exit(23)
""", str(models.engine.url.database)], capture_output=True)
    assert crash.returncode == 23, crash.stderr.decode(errors="replace")
    assert job_row(factory, jid).locked_by == "crashed-worker"
    worker = jobs.Worker(store=store)
    with factory() as db:
        assert jobs.recover(db, worker.worker_id, startup=True) == 1
    assert worker.run_once()
    assert not source.exists() and recording.mp4_path("TAKE").exists()
    assert worker.run_once()
    assert store.exists(storage.recording_key("TAKE"))
    playback = client.get("/sessions/TAKE/recording", headers=OP)
    assert playback.status_code == 200
    assert client.get(playback.json()["url"], headers={"Range": "bytes=0-15"}).status_code == 206


def test_backup_restores_real_sqlite(env, tmp_path):
    factory, _, store = env
    take(factory)
    queued(factory, "sqlite_backup", "2026-10-26")
    assert jobs.Worker(store=store).run_once()
    archived = store.path("backups/sqlite/2026-10-26/mindprobe.db.gz")
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(archived.read_bytes()))
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT id FROM sessions").fetchall() == [("TAKE",)]


def test_additive_migration_preserves_legacy_row(env):
    factory, _, _ = env
    take(factory)
    # Simulate the actual predecessor schema by removing only this batch's columns.
    with models.engine.begin() as conn:
        for column in ("recording_key", "recording_sha256", "manifest", "deleted_by", "deleted_at"):
            conn.exec_driver_sql(f"ALTER TABLE recordings DROP COLUMN {column}")
    models.init_db()
    models.init_db()  # idempotent startup
    with factory() as db:
        rec = models.get_recording(db, "TAKE")
        assert rec.final_sequence == 0 and rec.recording_key is None


def test_s3_adapter_checks_metadata_and_presigns_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("MINDPROBE_PRESIGN_TTL_SECONDS", raising=False)
    from botocore.exceptions import ClientError
    class Fake:
        def __init__(self): self.objects = {}
        def upload_file(self, filename, bucket, key, **kwargs):
            self.objects[key] = (Path(filename).read_bytes(), kwargs["ExtraArgs"]["Metadata"])
        def head_object(self, **kwargs):
            if kwargs["Key"] not in self.objects:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"Metadata": self.objects[kwargs["Key"]][1]}
        def delete_object(self, **kwargs): self.objects.pop(kwargs["Key"], None)
        def generate_presigned_url(self, action, **kwargs):
            assert action == "get_object" and kwargs["ExpiresIn"] == 900
            assert kwargs["Params"] == {"Bucket": "private", "Key": "sessions/TAKE/student.mp4"}
            return "https://example.invalid/signed"
    store = storage.S3Storage("private", client=Fake())
    key = storage.recording_key("TAKE")
    assert not store.exists(key)
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"movie")
    digest = storage.file_sha256(source)
    store.put_file(key, source, digest)
    assert store.exists(key, digest) and not store.exists(key, "wrong")
    assert store.presign_get(key) == "https://example.invalid/signed"
    store.delete(key)
    store.delete(key)
    assert not store.exists(key)


@pytest.mark.parametrize("key", ["../escape", "/escape", "C:/escape", "a/../b", "a\\b"])
def test_local_storage_rejects_unsafe_keys(tmp_path, key):
    with pytest.raises(ValueError): storage.LocalStorage(tmp_path).path(key)
