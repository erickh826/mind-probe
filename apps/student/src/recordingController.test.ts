import "fake-indexeddb/auto";

import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "./apiError.ts";
import {
  commitRecorderStop,
  handleRecorderStop,
  installActiveRecorderForTests,
  notePageHide,
  persistBlob,
  pumpSession,
  resetRecordingControllerForTests,
  setRecordingTransport,
} from "./recordingController.ts";
import { openRecordingDb, readMeta, writeMeta } from "./recordingDb.ts";
import { emptyMeta, persistStop, type RecordingMeta } from "./uploadQueue.ts";

function blob(text: string): Blob {
  return new Blob([text], { type: "video/webm" });
}

function deferred(): { promise: Promise<void>; entered: Promise<void>; arrive: () => void; release: () => void } {
  let arrive: () => void = () => {};
  let release: () => void = () => {};
  const entered = new Promise<void>((resolve) => {
    arrive = resolve;
  });
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, entered, arrive, release };
}

async function seed(sessionId: string, meta: RecordingMeta): Promise<void> {
  const database = await openRecordingDb();
  try {
    await writeMeta(database, meta);
  } finally {
    database.close();
  }
}

async function load(sessionId: string): Promise<RecordingMeta | null> {
  const database = await openRecordingDb();
  try {
    return await readMeta(database, sessionId);
  } finally {
    database.close();
  }
}

test("a status write keeps next_sequence and the stop recorded during the upload", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-race";
  await seed(sessionId, emptyMeta(sessionId, 1_000));
  await persistBlob(sessionId, blob("a"));

  const upload = deferred();
  const status = deferred();
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      upload.arrive();
      await upload.promise;
      return { status: "received" };
    },
    fetchUploadStatus: async () => {
      status.arrive();
      await status.promise;
      return {
        received_sequences: [0],
        next_expected_sequence: 1,
        final_sequence: null,
        finalize_state: "pending",
        recording_state: "finalizing",
      };
    },
    finalizeRecording: async () => {
      throw new Error("finalize must wait until the pending slices are the ones just stored");
    },
  });

  const pumping = pumpSession(sessionId);
  await upload.entered;
  await persistBlob(sessionId, blob("b"));
  const stopped = await commitRecorderStop(sessionId, 5_000);
  assert.equal(stopped?.next_sequence, 2);
  assert.equal(stopped?.recorder_state, "stopped");
  assert.equal(stopped?.finalize_payload?.final_sequence, 1);

  upload.release();
  await status.entered;
  await persistBlob(sessionId, blob("c"));
  status.release();
  await pumping;

  const meta = await load(sessionId);
  assert.equal(meta?.next_sequence, 3);
  assert.equal(meta?.recorder_state, "stopped");
  assert.equal(meta?.finalize_payload?.final_sequence, 1);
  assert.equal(meta?.finalize_payload?.total_chunks, 2);
  assert.equal(meta?.finalize_payload?.reason, undefined);
  assert.equal(meta?.server_finalize_state, "pending");
});

test("a finalize response does not rewind a chunk stored while the POST is in flight", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-fin";
  await seed(
    sessionId,
    persistStop({ ...emptyMeta(sessionId, 0), next_sequence: 1, last_chunk_end_ms: 2_000 }, 0),
  );

  const finalize = deferred();
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      throw new Error("the queue is empty");
    },
    fetchUploadStatus: async () => {
      throw new Error("status is not part of this post");
    },
    finalizeRecording: async () => {
      finalize.arrive();
      await finalize.promise;
      return {
        recording_state: "complete",
        finalize_state: "complete" as const,
        final_sequence: 0,
        missing_sequences: [],
      };
    },
  });

  const pumping = pumpSession(sessionId);
  await finalize.entered;
  await persistBlob(sessionId, blob("late"));
  finalize.release();
  await pumping;

  const meta = await load(sessionId);
  assert.equal(meta?.next_sequence, 2);
  assert.equal(meta?.finalize_payload?.final_sequence, 0);
  assert.equal(meta?.finalize_payload?.total_chunks, 1);
  assert.equal(meta?.finalize_confirmed, true);
  assert.equal(meta?.recorder_state, "stopped");
});

test("pagehide records recorder_lost instead of a normal finalize", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-hide";
  await seed(sessionId, {
    ...emptyMeta(sessionId, 10),
    next_sequence: 2,
    last_chunk_end_ms: 4_000,
  });

  let done: Promise<void> = Promise.resolve();
  const recorder = {
    state: "recording",
    stop() {
      this.state = "inactive";
      done = handleRecorderStop(sessionId, null);
    },
  };
  installActiveRecorderForTests(sessionId, recorder);
  notePageHide();
  await done;

  const meta = await load(sessionId);
  assert.equal(recorder.state, "inactive");
  assert.equal(meta?.recorder_state, "lost");
  assert.equal(meta?.finalize_payload?.reason, "recorder_lost");
  assert.equal(meta?.finalize_payload?.final_sequence, 1);
  assert.equal(meta?.next_sequence, 2);
});

test("a user stop records a normal finalize", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-stop";
  await seed(sessionId, {
    ...emptyMeta(sessionId, 10),
    next_sequence: 2,
    last_chunk_end_ms: 4_000,
  });
  const saved = await commitRecorderStop(sessionId, 90);
  assert.equal(saved?.recorder_state, "stopped");
  assert.equal(saved?.finalize_payload?.reason, undefined);
  assert.equal(saved?.finalize_payload?.final_sequence, 1);
  assert.equal(saved?.finalize_pending, true);
});

test("sequence_conflict is not uploaded again and flags the operator", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-409";
  await seed(sessionId, emptyMeta(sessionId, 1));
  await persistBlob(sessionId, blob("a"));

  let calls = 0;
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      calls += 1;
      throw new ApiError(409, "conflict", "sequence_conflict");
    },
    fetchUploadStatus: async () => {
      throw new Error("status unused");
    },
    finalizeRecording: async () => {
      throw new Error("finalize unused");
    },
  });

  await pumpSession(sessionId);
  await pumpSession(sessionId);
  assert.equal(calls, 1);

  const meta = await load(sessionId);
  assert.equal(meta?.needs_operator, true);
  assert.equal(meta?.upload_block, "sequence_conflict");
  assert.equal(meta?.next_sequence, 1);
  assert.equal(meta?.finalize_pending, false);
});

test("a permanent finalize 409 is not sent again and flags the operator", async () => {
  for (const [sessionId, code] of [
    ["S-fin-conflict", "finalize_conflict"],
    ["S-fin-deleted", "recording_deleted"],
  ] as const) {
    await resetRecordingControllerForTests();
    await seed(
      sessionId,
      persistStop({ ...emptyMeta(sessionId, 0), next_sequence: 1, last_chunk_end_ms: 2_000 }, 0),
    );

    let calls = 0;
    setRecordingTransport({
      uploadRecordingChunk: async () => {
        throw new Error("the queue is empty");
      },
      fetchUploadStatus: async () => {
        throw new Error("status unused");
      },
      finalizeRecording: async () => {
        calls += 1;
        throw new ApiError(409, "conflict", code);
      },
    });

    await pumpSession(sessionId);
    await pumpSession(sessionId);
    assert.equal(calls, 1, code);

    const meta = await load(sessionId);
    assert.equal(meta?.needs_operator, true);
    assert.equal(meta?.upload_block, code);
    assert.equal(meta?.finalize_pending, false);
  }
});
