import "fake-indexeddb/auto";

import assert from "node:assert/strict";
import { afterEach } from "node:test";
import test from "node:test";
import { openRecordingDb, writeChunk, writeMeta } from "./recordingDb.ts";
import { findUnfinishedUploadSessions } from "./uploadRecovery.ts";
import {
  applyServerStatus,
  emptyMeta,
  persistRecorderLost,
  notePermanentFailure,
  persistInterrupted,
  persistStop,
  type RecordingMeta,
} from "./uploadQueue.ts";

afterEach(async () => {
  await new Promise<void>((resolve) => {
    const request = indexedDB.deleteDatabase("mindprobe-recording");
    request.onsuccess = () => resolve();
    request.onerror = () => resolve();
    request.onblocked = () => resolve();
  });
});

async function save(meta: RecordingMeta): Promise<void> {
  const database = await openRecordingDb();
  try {
    await writeMeta(database, meta);
  } finally {
    database.close();
  }
}

test("no IndexedDB row keeps the normal join form", async () => {
  assert.deepEqual(await findUnfinishedUploadSessions(), []);
});

test("a pending finalize with no chunks is an upload-code entry", async () => {
  const meta = persistStop(emptyMeta("S-finalize-only", 1_000), 2_000);
  assert.equal(meta.finalize_pending, true);
  await save(meta);
  assert.deepEqual(await findUnfinishedUploadSessions(), [{ sessionId: "S-finalize-only" }]);
});

test("a confirmed complete take is not an upload-code entry", async () => {
  const meta = persistStop(emptyMeta("S-done", 1_000), 2_000);
  meta.finalize_confirmed = true;
  meta.finalize_pending = false;
  meta.server_finalize_state = "complete";
  await save(meta);
  assert.deepEqual(await findUnfinishedUploadSessions(), []);
});

test("a retained incomplete take with nothing left to send does not force the upload form", async () => {
  const settled = applyServerStatus(
    persistRecorderLost(emptyMeta("S-kept", 1_000), 2_000),
    { finalize_state: "incomplete", recording_state: "incomplete", received_sequences: [] },
    3_000,
  );
  assert.equal(settled.recorder_state, "lost");
  assert.equal(settled.finalize_pending, false);
  assert.equal(settled.server_finalize_state, "incomplete");
  await save(settled);
  const database = await openRecordingDb();
  try {
    await writeChunk(database, {
      session_id: "S-kept",
      sequence: 0,
      start_ms: 0,
      end_ms: 1000,
      checksum: "conflict",
      blob: new Blob(["conflict"]),
      attempts: 1,
      next_attempt_at: Number.MAX_SAFE_INTEGER,
      terminal: true,
      terminal_reason: "sequence_conflict",
    });
  } finally {
    database.close();
  }
  assert.deepEqual(await findUnfinishedUploadSessions(), []);

  await save(persistStop(emptyMeta("S-newer", 4_000), 5_000));
  assert.deepEqual(await findUnfinishedUploadSessions(), [{ sessionId: "S-newer" }]);
});

test("a revoked take whose only remaining work is its blocked finalize is an upload-code entry", async () => {
  // Every slice reached the server; the recorder_lost finalize got 401 and stopped.
  const lost = persistInterrupted({ ...emptyMeta("S-revoked-finalize", 1_000), next_sequence: 2 }, 2_000);
  const blocked = notePermanentFailure(lost, "unauthorized");
  assert.equal(blocked.finalize_pending, false);
  await save(blocked);
  assert.deepEqual(await findUnfinishedUploadSessions(), [{ sessionId: "S-revoked-finalize" }]);
});
