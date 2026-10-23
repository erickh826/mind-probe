import assert from "node:assert/strict";
import test from "node:test";
import {
  FINALIZE_DELAY_MS,
  META_RETENTION_MS,
  MemoryUploadLog,
  acknowledgeChunk,
  applyServerStatus,
  clearUnauthorizedChunk,
  clearUnauthorizedUploadBlock,
  closeBanner,
  emptyMeta,
  showUploadCodeEntry,
  finalizeDue,
  noteFinalizeAttempt,
  notePermanentFailure,
  permanentUploadBlock,
  persistRecorderLost,
  persistStop,
  planReload,
  sequencesAlreadyReceived,
  shouldDiscardMeta,
} from "./uploadQueue.ts";

test("next_sequence advances with the chunk and survives ACK", () => {
  const log = new MemoryUploadLog("S1");
  assert.equal(log.put(1000), 0);
  assert.equal(log.put(2000), 1);
  assert.equal(log.put(3000), 2);
  assert.equal(log.meta.next_sequence, 3);

  log.ack(0);
  log.ack(1);
  log.ack(2);
  assert.equal(log.chunks.size, 0);
  assert.equal(log.meta.next_sequence, 3);
  assert.equal(log.meta.session_id, "S1");

  // The queue is empty, so the next slice must not restart at 0.
  assert.equal(log.put(4000), 3);
  assert.equal(log.meta.next_sequence, 4);
  assert.deepEqual(acknowledgeChunk(log.meta).next_sequence, 4);
});

test("finalize sends immediately when the queue is empty, else at 30s", () => {
  const started = 1_000_000;
  let meta = persistStop(emptyMeta("S1", started), started);
  meta = { ...meta, next_sequence: 2, finalize_payload: meta.finalize_payload };

  assert.equal(finalizeDue(meta, 0, started), true);
  assert.equal(finalizeDue(meta, 3, started + FINALIZE_DELAY_MS - 1), false);
  assert.equal(finalizeDue(meta, 3, started + FINALIZE_DELAY_MS), true);

  const retried = noteFinalizeAttempt(meta, started + 500, true);
  assert.equal(retried.finalize_persisted_at, meta.finalize_persisted_at);
  assert.equal(finalizeDue(retried, 3, started + FINALIZE_DELAY_MS), true);
  assert.equal(
    finalizeDue(retried, 3, started + FINALIZE_DELAY_MS - 1),
    false,
  );
});

test("reload resumes or declares recorder_lost and does not start a take", () => {
  const recording = emptyMeta("S1", 10);
  assert.equal(planReload(recording).kind, "recorder_lost");
  const lost = persistRecorderLost(recording, 50);
  assert.equal(lost.recorder_state, "lost");
  assert.equal(lost.finalize_payload?.reason, "recorder_lost");
  assert.equal(lost.finalize_payload?.final_sequence, -1);
  assert.equal(lost.finalize_persisted_at, 50);

  const stopped = persistStop({ ...recording, next_sequence: 4, last_chunk_end_ms: 8000 }, 90);
  assert.equal(stopped.finalize_payload?.final_sequence, 3);
  assert.equal(stopped.finalize_payload?.total_chunks, 4);
  assert.equal(planReload(stopped).kind, "resume_uploads");

  const confirmed = applyServerStatus(
    stopped,
    { finalize_state: "complete", recording_state: "complete", received_sequences: [0, 1, 2, 3] },
    100,
  );
  assert.equal(confirmed.finalize_confirmed, true);
  assert.equal(planReload(confirmed).kind, "safe_to_close");
  assert.equal(planReload(null).kind, "idle");
});

test("meta is kept 7 days after complete and never auto-cleared otherwise", () => {
  const now = 10_000_000;
  const base = persistStop(emptyMeta("S1", now), now);
  const pending = { ...base, server_finalize_state: "pending" as const };
  assert.equal(shouldDiscardMeta(pending, now + META_RETENTION_MS + 1), false);

  const incomplete = applyServerStatus(
    base,
    { finalize_state: "incomplete", recording_state: "incomplete", received_sequences: [] },
    now,
  );
  assert.equal(shouldDiscardMeta(incomplete, now + META_RETENTION_MS * 2), false);

  const lost = persistRecorderLost(emptyMeta("S1", now - META_RETENTION_MS), now - META_RETENTION_MS);
  assert.equal(shouldDiscardMeta(lost, now + META_RETENTION_MS), false);

  const confirmed = applyServerStatus(
    base,
    { finalize_state: "complete", recording_state: "complete", received_sequences: [0] },
    now,
  );
  assert.equal(shouldDiscardMeta(confirmed, now + META_RETENTION_MS - 1), false);
  assert.equal(shouldDiscardMeta(confirmed, now + META_RETENTION_MS), true);
});

test("server receipts drop local chunks without touching the counter", () => {
  const log = new MemoryUploadLog("S1");
  log.put(1000);
  log.put(2000);
  const drop = sequencesAlreadyReceived([0, 1], [0]);
  for (const sequence of drop) {
    log.ack(sequence);
  }
  assert.deepEqual([...log.chunks.keys()], [1]);
  assert.equal(log.meta.next_sequence, 2);
});

test("safe to close only after the server confirms complete", () => {
  const meta = persistStop({ ...emptyMeta("S1", 0), next_sequence: 1 }, 5_000);
  assert.equal(closeBanner(meta, 2).kind, "uploading");
  assert.equal(closeBanner(meta, 0).kind, "confirming");
  const confirmed = applyServerStatus(
    meta,
    { finalize_state: "complete", recording_state: "complete", received_sequences: [0] },
    6_000,
  );
  assert.equal(closeBanner(confirmed, 0).kind, "safe");
  const lost = persistRecorderLost(emptyMeta("S1", 0), 1);
  assert.equal(closeBanner(lost, 1).kind, "interrupted");
  const incomplete = applyServerStatus(
    meta,
    { finalize_state: "incomplete", recording_state: "incomplete", received_sequences: [] },
    7_000,
  );
  assert.equal(closeBanner(incomplete, 0).kind, "incomplete");
});

test("applyServerStatus does not touch sequence, recorder state, or the payload", () => {
  const meta = persistStop(
    { ...emptyMeta("S1", 0), next_sequence: 4, last_chunk_end_ms: 9000 },
    5_000,
  );
  const next = applyServerStatus(
    meta,
    { finalize_state: "complete", recording_state: "complete", received_sequences: [0, 1, 2, 3] },
    6_000,
  );
  assert.equal(next.next_sequence, 4);
  assert.equal(next.recorder_state, "stopped");
  assert.deepEqual(next.finalize_payload, meta.finalize_payload);
  assert.equal(next.finalize_confirmed, true);
  assert.equal(next.finalize_pending, false);
  assert.equal(next.server_finalize_state, "complete");
  assert.equal(next.finalize_persisted_at, meta.finalize_persisted_at);

  const stale = applyServerStatus(
    next,
    { finalize_state: "pending", recording_state: "finalizing", received_sequences: [] },
    7_000,
  );
  assert.equal(stale.server_finalize_state, "complete");
  assert.equal(stale.finalize_confirmed, true);
  assert.equal(stale.finalize_persisted_at, meta.finalize_persisted_at);
});

test("permanent 4xx stops the matching retry and flags the operator", () => {
  assert.equal(permanentUploadBlock(409, "sequence_conflict"), "sequence_conflict");
  assert.equal(permanentUploadBlock(409, "sequence_past_final"), "sequence_past_final");
  assert.equal(permanentUploadBlock(409, "finalize_conflict"), "finalize_conflict");
  assert.equal(permanentUploadBlock(409, "recording_deleted"), "recording_deleted");
  assert.equal(permanentUploadBlock(400, "checksum_mismatch"), "checksum");
  assert.equal(permanentUploadBlock(401, "revoked_session_token"), "unauthorized");
  assert.equal(permanentUploadBlock(422, null), "invalid_finalize");
  assert.equal(permanentUploadBlock(503, null), null);

  const meta = persistStop(emptyMeta("S1", 0), 1_000);
  const blocked = notePermanentFailure(meta, "sequence_conflict");
  assert.equal(blocked.needs_operator, true);
  assert.equal(blocked.upload_block, "sequence_conflict");
  assert.equal(blocked.finalize_pending, true);
  assert.equal(blocked.next_sequence, meta.next_sequence);
  assert.deepEqual(blocked.finalize_payload, meta.finalize_payload);

  const expired = notePermanentFailure(meta, "unauthorized");
  assert.equal(expired.finalize_pending, false);
  assert.equal(closeBanner(blocked, 1).kind, "needs_operator");

  for (const block of ["finalize_conflict", "recording_deleted"] as const) {
    const stopped = notePermanentFailure(meta, block);
    assert.equal(stopped.needs_operator, true);
    assert.equal(stopped.finalize_pending, false);
    assert.equal(stopped.finalize_next_attempt_at, null);
    assert.equal(closeBanner(stopped, 0).kind, "needs_operator");
  }
});

test("upload-login clears only an unauthorized block and keeps the finalize clock", () => {
  const stopped = persistStop(
    { ...emptyMeta("S-auth", 0), next_sequence: 3, last_chunk_end_ms: 4000 },
    5_000,
  );
  const blocked = notePermanentFailure(stopped, "unauthorized");
  const cleared = clearUnauthorizedUploadBlock(blocked, 9_000);
  assert.equal(cleared.upload_block, null);
  assert.equal(cleared.needs_operator, false);
  assert.equal(cleared.finalize_pending, true);
  assert.equal(cleared.next_sequence, 3);
  assert.equal(cleared.finalize_persisted_at, stopped.finalize_persisted_at);
  assert.deepEqual(cleared.finalize_payload, stopped.finalize_payload);
  assert.equal(cleared.recorder_state, "stopped");

  const conflict = notePermanentFailure(stopped, "sequence_conflict");
  const stillConflict = clearUnauthorizedUploadBlock(conflict, 9_000);
  assert.equal(stillConflict, conflict);
  assert.equal(stillConflict.upload_block, "sequence_conflict");
  assert.equal(stillConflict.needs_operator, true);

  const lost = notePermanentFailure(
    persistRecorderLost({ ...emptyMeta("S-lost", 0), next_sequence: 2, last_chunk_end_ms: 1000 }, 2_000),
    "unauthorized",
  );
  const clearedLost = clearUnauthorizedUploadBlock(lost, 9_000);
  assert.equal(clearedLost.recorder_state, "lost");
  assert.equal(clearedLost.needs_operator, true);
  assert.equal(clearedLost.upload_block, null);
  assert.equal(clearedLost.finalize_payload?.reason, "recorder_lost");
  assert.equal(clearedLost.finalize_persisted_at, lost.finalize_persisted_at);
  assert.equal(clearedLost.next_sequence, lost.next_sequence);
  assert.notEqual(closeBanner(clearedLost, 0).kind, "safe");
});

test("unauthorized chunks retry and other terminal chunks stay put", () => {
  const unauthorized = {
    terminal: true,
    terminal_reason: "unauthorized" as const,
    next_attempt_at: Number.MAX_SAFE_INTEGER,
    sequence: 1,
  };
  const opened = clearUnauthorizedChunk(unauthorized, 50);
  assert.equal(opened.terminal, false);
  assert.equal(opened.next_attempt_at, 50);
  assert.equal(opened.sequence, 1);

  const conflict = {
    terminal: true,
    terminal_reason: "sequence_conflict" as const,
    next_attempt_at: Number.MAX_SAFE_INTEGER,
    sequence: 0,
  };
  assert.equal(clearUnauthorizedChunk(conflict, 50), conflict);
});

test("upload code entry is the unfinished take, including finalize with no chunks", () => {
  assert.equal(showUploadCodeEntry(null, 0), false);
  const pendingFinalize = persistStop(emptyMeta("S-fin", 0), 1_000);
  assert.equal(pendingFinalize.finalize_pending, true);
  assert.equal(showUploadCodeEntry(pendingFinalize, 0), true);
  const complete = applyServerStatus(
    pendingFinalize,
    { finalize_state: "complete", recording_state: "complete", received_sequences: [] },
    2_000,
  );
  assert.equal(showUploadCodeEntry(complete, 0), false);
  assert.equal(closeBanner(pendingFinalize, 0).kind === "safe", false);
  assert.equal(closeBanner(complete, 0).kind, "safe");
  const lost = persistRecorderLost(emptyMeta("S-lost", 0), 1_000);
  assert.equal(showUploadCodeEntry(lost, 0), true);
  assert.equal(closeBanner(lost, 0).kind, "interrupted");
});
