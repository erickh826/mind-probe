import "fake-indexeddb/auto";

import assert from "node:assert/strict";
import { afterEach } from "node:test";
import test from "node:test";
import { openRecordingDb, writeMeta } from "./recordingDb.ts";
import { findUnfinishedUploadSession } from "./uploadRecovery.ts";
import { emptyMeta, persistStop, type RecordingMeta } from "./uploadQueue.ts";

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
  assert.equal(await findUnfinishedUploadSession(), null);
});

test("a pending finalize with no chunks is an upload-code entry", async () => {
  const meta = persistStop(emptyMeta("S-finalize-only", 1_000), 2_000);
  assert.equal(meta.finalize_pending, true);
  await save(meta);
  assert.deepEqual(await findUnfinishedUploadSession(), { sessionId: "S-finalize-only" });
});

test("a confirmed complete take is not an upload-code entry", async () => {
  const meta = persistStop(emptyMeta("S-done", 1_000), 2_000);
  meta.finalize_confirmed = true;
  meta.finalize_pending = false;
  meta.server_finalize_state = "complete";
  await save(meta);
  assert.equal(await findUnfinishedUploadSession(), null);
});
