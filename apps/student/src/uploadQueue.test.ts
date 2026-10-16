import assert from "node:assert/strict";
import test from "node:test";
import {
  FINALIZE_DELAY_MS,
  META_RETENTION_MS,
  MemoryUploadLog,
  acknowledgeChunk,
  applyServerStatus,
  closeBanner,
  emptyMeta,
  finalizeDue,
  noteFinalizeAttempt,
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
