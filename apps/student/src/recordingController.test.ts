import "fake-indexeddb/auto";

import assert from "node:assert/strict";
import { afterEach } from "node:test";
import test from "node:test";
import { ApiError } from "./apiError.ts";
import {
  attachRecorderForTests,
  bindSession,
  commitRecorderStop,
  handleRecorderStop,
  installActiveRecorderForTests,
  notePageHide,
  persistBlob,
  pumpSession,
  recorderStopPromiseForTests,
  recordingView,
  resetRecordingControllerForTests,
  resumeUnauthorizedUploads,
  setRecordingTransport,
} from "./recordingController.ts";
import { listChunks, openRecordingDb, readMeta, writeChunk, writeMeta } from "./recordingDb.ts";
import {
  emptyMeta,
  notePermanentFailure,
  permanentBlockMessage,
  persistRecorderLost,
  persistStop,
  type RecordingMeta,
  type ServerUploadStatus,
} from "./uploadQueue.ts";

afterEach(async () => {
  await resetRecordingControllerForTests();
});

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

async function seedChunk(
  sessionId: string,
  sequence: number,
  text: string,
  nextAttemptAt = 0,
): Promise<void> {
  const database = await openRecordingDb();
  try {
    await writeChunk(database, {
      session_id: sessionId,
      sequence,
      start_ms: sequence * 1_000,
      end_ms: sequence * 1_000 + 1_000,
      checksum: `sum-${text}`,
      blob: blob(text),
      attempts: 0,
      next_attempt_at: nextAttemptAt,
    });
  } finally {
    database.close();
  }
}

async function releaseChunk(sessionId: string, sequence: number): Promise<void> {
  const database = await openRecordingDb();
  try {
    const chunks = await listChunks(database, sessionId);
    const chunk = chunks.find((item) => item.sequence === sequence);
    if (!chunk) {
      throw new Error(`missing chunk ${sequence}`);
    }
    await writeChunk(database, { ...chunk, next_attempt_at: 0 });
  } finally {
    database.close();
  }
}

function liveStatus(received: number[], finalize: ServerUploadStatus["finalize_state"] = "none"): ServerUploadStatus {
  return {
    received_sequences: received,
    finalize_state: finalize,
    recording_state: "recording",
  };
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

test("finalize starts at the deadline while upload and status are still pending", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-deadline";
  await seed(sessionId, emptyMeta(sessionId, 0));
  await persistBlob(sessionId, blob("pending"));
  const stored = await load(sessionId);
  assert.ok(stored);
  const persistedAt = Date.now() - 31_000;
  await seed(sessionId, persistStop(stored, persistedAt));

  const upload = deferred();
  const status = deferred();
  const finalize = deferred();
  let statusResolved = false;
  let finalizeCalls = 0;
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      upload.arrive();
      await upload.promise;
      return { status: "received" };
    },
    fetchUploadStatus: async () => {
      status.arrive();
      await status.promise;
      statusResolved = true;
      return {
        finalize_state: "pending",
        recording_state: "finalizing",
        received_sequences: [],
      };
    },
    finalizeRecording: async () => {
      finalizeCalls += 1;
      finalize.arrive();
      await finalize.promise;
      return {
        recording_state: "finalizing",
        finalize_state: "pending",
        final_sequence: 0,
        missing_sequences: [0],
      };
    },
  });

  const first = pumpSession(sessionId);
  await upload.entered;
  const second = pumpSession(sessionId);
  await finalize.entered;
  assert.equal(finalizeCalls, 1);
  assert.equal(statusResolved, false);
  const during = await load(sessionId);
  assert.equal(during?.finalize_persisted_at, persistedAt);

  upload.release();
  await status.entered;
  status.release();
  finalize.release();
  await first;
  await second;
  const after = await load(sessionId);
  assert.equal(after?.finalize_persisted_at, persistedAt);
});

test("a hung status read after upload does not hold the finalize deadline", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-status-hang";
  await seed(sessionId, emptyMeta(sessionId, 0));
  await persistBlob(sessionId, blob("pending"));
  const stored = await load(sessionId);
  assert.ok(stored);
  const persistedAt = Date.now() - 31_000;
  await seed(sessionId, persistStop(stored, persistedAt));

  const status = deferred();
  const finalize = deferred();
  let statusResolved = false;
  setRecordingTransport({
    uploadRecordingChunk: async () => ({ status: "received" }),
    fetchUploadStatus: async () => {
      status.arrive();
      await status.promise;
      statusResolved = true;
      return {
        finalize_state: "pending",
        recording_state: "finalizing",
        received_sequences: [0],
      };
    },
    finalizeRecording: async () => {
      finalize.arrive();
      return {
        recording_state: "finalizing",
        finalize_state: "pending",
        final_sequence: 0,
        missing_sequences: [],
      };
    },
  });

  const pumping = pumpSession(sessionId);
  await status.entered;
  await finalize.entered;
  assert.equal(statusResolved, false);
  const during = await load(sessionId);
  assert.equal(during?.finalize_persisted_at, persistedAt);
  status.release();
  await pumping;
});

test("a failed finalize retry does not move finalize_persisted_at", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-deadline-retry";
  const persistedAt = 5_000;
  await seed(
    sessionId,
    persistStop({ ...emptyMeta(sessionId, 0), next_sequence: 1, last_chunk_end_ms: 1_000 }, persistedAt),
  );
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      throw new Error("no chunk");
    },
    fetchUploadStatus: async () => {
      throw new Error("status unused");
    },
    finalizeRecording: async () => {
      throw new ApiError(503, "unavailable", null);
    },
  });
  await pumpSession(sessionId);
  const meta = await load(sessionId);
  assert.equal(meta?.finalize_persisted_at, persistedAt);
  assert.equal(meta?.finalize_pending, true);
});

test("reload finalizes while the first status reconciliation is still pending", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-restore-stall";
  const persistedAt = Date.now() - 31_000;
  await seed(
    sessionId,
    persistStop({ ...emptyMeta(sessionId, 0), next_sequence: 1, last_chunk_end_ms: 1_000 }, persistedAt),
  );

  const status = deferred();
  let statusResolved = false;
  const finalize = deferred();
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      throw new Error("no chunk is waiting");
    },
    fetchUploadStatus: async () => {
      status.arrive();
      await status.promise;
      statusResolved = true;
      return {
        finalize_state: "pending",
        recording_state: "finalizing",
        received_sequences: [],
      };
    },
    finalizeRecording: async () => {
      finalize.arrive();
      return {
        recording_state: "finalizing",
        finalize_state: "pending",
        final_sequence: 0,
        missing_sequences: [],
      };
    },
  });

  const restoring = bindSession(sessionId);
  await finalize.entered;
  assert.equal(statusResolved, false);
  const during = await load(sessionId);
  assert.equal(during?.finalize_persisted_at, persistedAt);
  status.release();
  await restoring;
  const after = await load(sessionId);
  assert.equal(after?.finalize_persisted_at, persistedAt);
});

function failChunkStorePuts(times: number): () => void {
  const original = IDBObjectStore.prototype.put;
  let left = times;
  IDBObjectStore.prototype.put = function (
    this: IDBObjectStore,
    value: unknown,
    key?: IDBValidKey,
  ): IDBRequest<IDBValidKey> {
    const isChunk = Boolean(value && typeof value === "object" && "blob" in value);
    if (isChunk && (times < 0 || left > 0)) {
      if (times >= 0) {
        left -= 1;
      }
      throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
    }
    return original.call(this, value as never, key);
  };
  return () => {
    IDBObjectStore.prototype.put = original;
  };
}

test("a transient IndexedDB failure is retried and still stops normally", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-quota-once";
  await seed(sessionId, emptyMeta(sessionId, 1));
  const restore = failChunkStorePuts(1);
  try {
    const recorder = {
      state: "recording",
      ondataavailable: null as ((event: { data: Blob }) => void) | null,
      onstop: null as (() => void) | null,
      onerror: null as (() => void) | null,
      stop() {
        this.state = "inactive";
      },
    };
    attachRecorderForTests(sessionId, recorder as unknown as MediaRecorder);
    recorder.ondataavailable?.({ data: blob("kept") });
    recorder.onstop?.();
    await recorderStopPromiseForTests();
  } finally {
    restore();
  }
  const meta = await load(sessionId);
  assert.equal(meta?.recorder_state, "stopped");
  assert.equal(meta?.needs_operator, false);
  assert.equal(meta?.finalize_payload?.reason, undefined);
  assert.equal(meta?.next_sequence, 1);
});

test("an IndexedDB failure fail-closes the take across stop and reload", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-quota";
  await seed(sessionId, emptyMeta(sessionId, 1));
  await persistBlob(sessionId, blob("kept"));

  const restorePuts = failChunkStorePuts(-1);
  try {
    const recorder = {
      state: "recording",
      ondataavailable: null as ((event: { data: Blob }) => void) | null,
      onstop: null as (() => void) | null,
      onerror: null as (() => void) | null,
      stop() {
        this.state = "inactive";
      },
    };
    attachRecorderForTests(sessionId, recorder as unknown as MediaRecorder);
    recorder.ondataavailable?.({ data: blob("lost") });
    recorder.onstop?.();
    await recorderStopPromiseForTests();
  } finally {
    restorePuts();
  }

  const stopped = await load(sessionId);
  assert.equal(stopped?.recorder_state, "lost");
  assert.equal(stopped?.needs_operator, true);
  assert.equal(stopped?.finalize_payload?.reason, "recorder_lost");
  assert.notEqual(stopped?.recorder_state, "stopped");

  await resetRecordingControllerForTests({ keepDatabase: true });
  setRecordingTransport({
    uploadRecordingChunk: async () => ({ status: "received" }),
    fetchUploadStatus: async () => ({
      finalize_state: "pending",
      recording_state: "finalizing",
      received_sequences: [],
    }),
    finalizeRecording: async () => ({
      recording_state: "incomplete",
      finalize_state: "incomplete",
      final_sequence: 0,
      missing_sequences: [],
    }),
  });
  await bindSession(sessionId);
  const reloaded = await load(sessionId);
  assert.equal(reloaded?.recorder_state, "lost");
  assert.equal(reloaded?.needs_operator, true);
  assert.equal(reloaded?.finalize_payload?.reason, "recorder_lost");
  assert.equal(recordingView().banner.kind, "interrupted");
});

test("upload-login reopens only unauthorized chunks and leaves conflict and lost state", async () => {
  const sessionId = "S-reauth";
  const stopped = persistStop(
    { ...emptyMeta(sessionId, 1_000), next_sequence: 2, last_chunk_end_ms: 3000 },
    4_000,
  );
  const blocked = {
    ...notePermanentFailure(stopped, "unauthorized"),
    upload_block: "unauthorized" as const,
  };
  const database = await openRecordingDb();
  await writeMeta(database, blocked);
  await writeChunk(database, {
    session_id: sessionId,
    sequence: 0,
    start_ms: 0,
    end_ms: 1000,
    checksum: "conflict",
    blob: blob("conflict"),
    attempts: 1,
    next_attempt_at: Number.MAX_SAFE_INTEGER,
    terminal: true,
    terminal_reason: "sequence_conflict",
  });
  await writeChunk(database, {
    session_id: sessionId,
    sequence: 1,
    start_ms: 1000,
    end_ms: 3000,
    checksum: "auth",
    blob: blob("auth"),
    attempts: 1,
    next_attempt_at: Number.MAX_SAFE_INTEGER,
    terminal: true,
    terminal_reason: "unauthorized",
  });
  database.close();

  const now = 8_000;
  const updated = await resumeUnauthorizedUploads(sessionId, now);
  assert.equal(updated?.needs_operator, true);
  assert.equal(updated?.upload_block, "sequence_conflict");
  assert.equal(updated?.finalize_pending, true);
  assert.equal(updated?.next_sequence, 2);
  assert.equal(updated?.finalize_persisted_at, stopped.finalize_persisted_at);
  assert.deepEqual(updated?.finalize_payload, stopped.finalize_payload);

  const savedDb = await openRecordingDb();
  const chunks = await listChunks(savedDb, sessionId);
  savedDb.close();
  const conflict = chunks.find((chunk) => chunk.sequence === 0);
  const unauthorized = chunks.find((chunk) => chunk.sequence === 1);
  assert.equal(conflict?.terminal, true);
  assert.equal(conflict?.terminal_reason, "sequence_conflict");
  assert.equal(unauthorized?.terminal, false);
  assert.equal(unauthorized?.next_attempt_at, now);

  const lost = notePermanentFailure(
    persistRecorderLost({ ...emptyMeta("S-lost-db", 0), next_sequence: 1, last_chunk_end_ms: 500 }, 2_000),
    "unauthorized",
  );
  await seed("S-lost-db", lost);
  const kept = await resumeUnauthorizedUploads("S-lost-db", now);
  assert.equal(kept?.recorder_state, "lost");
  assert.equal(kept?.needs_operator, true);
  assert.equal(kept?.upload_block, null);
  assert.equal(kept?.finalize_payload?.reason, "recorder_lost");
  assert.equal(kept?.finalize_persisted_at, lost.finalize_persisted_at);
  assert.equal(kept?.next_sequence, lost.next_sequence);
});

test("a recovered chunk upload clears that transient failure while recording continues", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-retry-clear";
  await seed(sessionId, { ...emptyMeta(sessionId, 1_000), next_sequence: 1, last_chunk_end_ms: 1_000 });
  await seedChunk(sessionId, 0, "slice");

  const uploadedSequences: number[] = [];
  let finalized = false;
  setRecordingTransport({
    uploadRecordingChunk: async (_sessionId, sequence) => {
      uploadedSequences.push(sequence);
      if (uploadedSequences.length === 1) {
        throw new TypeError("Failed to fetch");
      }
      return { status: "received" };
    },
    fetchUploadStatus: async () => liveStatus([0]),
    finalizeRecording: async () => {
      finalized = true;
      throw new Error("finalize must not run");
    },
  });

  await pumpSession(sessionId);
  assert.equal(recordingView().error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  assert.equal(recordingView().uploadedCount, 0);
  assert.equal(recordingView().failedCount, 1);
  assert.equal(recordingView().pendingCount, 1);

  await releaseChunk(sessionId, 0);
  await pumpSession(sessionId);

  const view = recordingView();
  assert.deepEqual(uploadedSequences, [0, 0]);
  assert.equal(view.uploadedCount, 1);
  assert.equal(view.failedCount, 0);
  assert.equal(view.pendingCount, 0);
  assert.equal(view.error, null);
  assert.equal(view.status === "stopped" || view.status === "stopping", false);
  assert.equal(finalized, false);

  const meta = await load(sessionId);
  assert.equal(meta?.recorder_state, "recording");
  assert.equal(meta?.next_sequence, 1);
  assert.equal(meta?.finalize_payload, null);
  assert.equal(meta?.finalize_persisted_at, null);
  assert.equal(meta?.finalize_pending, false);
  const database = await openRecordingDb();
  const remaining = await listChunks(database, sessionId);
  database.close();
  assert.equal(remaining.length, 0);
});

test("a retry the server has not confirmed keeps the transient failure", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-unconfirmed";
  await seed(sessionId, { ...emptyMeta(sessionId, 1_000), next_sequence: 1, last_chunk_end_ms: 1_000 });
  await seedChunk(sessionId, 0, "slice");
  let uploads = 0;
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      uploads += 1;
      if (uploads === 1) {
        throw new TypeError("Failed to fetch");
      }
      return { status: "received" };
    },
    fetchUploadStatus: async () => liveStatus([]),
    finalizeRecording: async () => {
      throw new Error("finalize must not run");
    },
  });

  await pumpSession(sessionId);
  await releaseChunk(sessionId, 0);
  await pumpSession(sessionId);

  const view = recordingView();
  assert.equal(view.uploadedCount, 1);
  assert.equal(view.failedCount, 0);
  assert.equal(view.pendingCount, 0);
  assert.equal(view.error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  const meta = await load(sessionId);
  assert.equal(meta?.next_sequence, 1);
  assert.equal(meta?.finalize_payload, null);
  assert.equal(meta?.finalize_persisted_at, null);
});

test("a successful upload does not clear a different chunk's transient failure", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-other-chunk";
  await seed(sessionId, { ...emptyMeta(sessionId, 1_000), next_sequence: 2, last_chunk_end_ms: 2_000 });
  await seedChunk(sessionId, 0, "first");
  await seedChunk(sessionId, 1, "second");
  const uploadedSequences: number[] = [];
  setRecordingTransport({
    uploadRecordingChunk: async (_sessionId, sequence) => {
      uploadedSequences.push(sequence);
      if (sequence === 0) {
        throw new TypeError("Failed to fetch");
      }
      return { status: "received" };
    },
    fetchUploadStatus: async () => liveStatus([1]),
    finalizeRecording: async () => {
      throw new Error("finalize must not run");
    },
  });

  await pumpSession(sessionId);
  assert.equal(recordingView().error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  await pumpSession(sessionId);

  const view = recordingView();
  assert.deepEqual(uploadedSequences, [0, 1]);
  assert.equal(view.uploadedCount, 1);
  assert.equal(view.failedCount, 1);
  assert.equal(view.pendingCount, 1);
  assert.equal(view.error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  const meta = await load(sessionId);
  assert.equal(meta?.next_sequence, 2);
  assert.equal(meta?.finalize_payload, null);
  assert.equal(meta?.finalize_persisted_at, null);
  assert.equal(meta?.recorder_state, "recording");
});

test("a successful upload does not clear a permanent sequence conflict", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-keep-conflict";
  await seed(sessionId, { ...emptyMeta(sessionId, 1_000), next_sequence: 2, last_chunk_end_ms: 2_000 });
  await seedChunk(sessionId, 0, "conflict");
  await seedChunk(sessionId, 1, "later");
  setRecordingTransport({
    uploadRecordingChunk: async (_sessionId, sequence) => {
      if (sequence === 0) {
        throw new ApiError(409, "conflict", "sequence_conflict");
      }
      return { status: "received" };
    },
    fetchUploadStatus: async () => liveStatus([1]),
    finalizeRecording: async () => {
      throw new Error("finalize must not run");
    },
  });

  await pumpSession(sessionId);
  await pumpSession(sessionId);

  const view = recordingView();
  assert.equal(view.error, permanentBlockMessage("sequence_conflict"));
  assert.equal(view.uploadedCount, 1);
  assert.equal(view.banner.kind, "needs_operator");
  const meta = await load(sessionId);
  assert.equal(meta?.needs_operator, true);
  assert.equal(meta?.upload_block, "sequence_conflict");
  assert.equal(meta?.next_sequence, 2);
  assert.equal(meta?.finalize_payload, null);
  assert.equal(meta?.finalize_persisted_at, null);
  const database = await openRecordingDb();
  const chunks = await listChunks(database, sessionId);
  database.close();
  const conflict = chunks.find((chunk) => chunk.sequence === 0);
  assert.equal(conflict?.terminal, true);
  assert.equal(conflict?.terminal_reason, "sequence_conflict");
  assert.equal(chunks.some((chunk) => chunk.sequence === 1), false);
});

test("a successful upload does not clear a storage failure or recorder_lost", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-keep-storage";
  await seed(sessionId, emptyMeta(sessionId, 1));
  await persistBlob(sessionId, blob("kept"));

  const restorePuts = failChunkStorePuts(-1);
  try {
    const recorder = {
      state: "recording",
      ondataavailable: null as ((event: { data: Blob }) => void) | null,
      onstop: null as (() => void) | null,
      onerror: null as (() => void) | null,
      stop() {
        this.state = "inactive";
      },
    };
    attachRecorderForTests(sessionId, recorder as unknown as MediaRecorder);
    recorder.ondataavailable?.({ data: blob("lost") });
    for (let attempt = 0; attempt < 20 && !recordingView().error; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  } finally {
    restorePuts();
  }

  const storageError = recordingView().error ?? "";
  assert.match(storageError, /^無法保存錄影片段：/);
  const lost = await load(sessionId);
  assert.equal(lost?.recorder_state, "lost");
  assert.equal(lost?.needs_operator, true);
  assert.equal(lost?.finalize_payload?.reason, "recorder_lost");
  assert.equal(lost?.next_sequence, 1);
  await seed(sessionId, { ...lost!, finalize_next_attempt_at: Number.MAX_SAFE_INTEGER });

  let finalized = false;
  setRecordingTransport({
    uploadRecordingChunk: async () => ({ status: "received" }),
    fetchUploadStatus: async () => liveStatus([0], "pending"),
    finalizeRecording: async () => {
      finalized = true;
      throw new Error("finalize must not run");
    },
  });
  await pumpSession(sessionId);

  assert.equal(finalized, false);
  assert.equal(recordingView().error, storageError);
  assert.equal(recordingView().banner.kind, "interrupted");
  assert.equal(recordingView().uploadedCount, 1);
  const after = await load(sessionId);
  assert.equal(after?.recorder_state, "lost");
  assert.equal(after?.needs_operator, true);
  assert.equal(after?.next_sequence, lost?.next_sequence);
  assert.equal(after?.finalize_persisted_at, lost?.finalize_persisted_at);
  assert.deepEqual(after?.finalize_payload, lost?.finalize_payload);
});

test("a successful upload does not clear a recorder failure", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-keep-recorder";
  await seed(sessionId, { ...emptyMeta(sessionId, 1_000), next_sequence: 1, last_chunk_end_ms: 1_000 });
  await seedChunk(sessionId, 0, "kept");
  const recorder = {
    state: "recording",
    ondataavailable: null as ((event: { data: Blob }) => void) | null,
    onstop: null as (() => void) | null,
    onerror: null as (() => void) | null,
    stop() {
      this.state = "inactive";
    },
  };
  attachRecorderForTests(sessionId, recorder as unknown as MediaRecorder);
  recorder.onerror?.();
  assert.equal(recordingView().error, "MediaRecorder error.");

  setRecordingTransport({
    uploadRecordingChunk: async () => ({ status: "received" }),
    fetchUploadStatus: async () => liveStatus([0]),
    finalizeRecording: async () => {
      throw new Error("finalize must not run");
    },
  });
  await pumpSession(sessionId);

  assert.equal(recordingView().error, "MediaRecorder error.");
  assert.equal(recordingView().uploadedCount, 1);
  assert.equal(recordingView().failedCount, 0);
  assert.equal(recordingView().pendingCount, 0);
  const meta = await load(sessionId);
  assert.equal(meta?.recorder_state, "recording");
  assert.equal(meta?.next_sequence, 1);
  assert.equal(meta?.finalize_payload, null);
  assert.equal(meta?.finalize_persisted_at, null);
});

test("a successful upload does not clear a finalize error", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-keep-finalize";
  const persistedAt = 5_000;
  await seed(
    sessionId,
    persistStop({ ...emptyMeta(sessionId, 0), next_sequence: 1, last_chunk_end_ms: 1_000 }, persistedAt),
  );
  await seedChunk(sessionId, 0, "late", Date.now() + 60_000);
  let finalizeCalls = 0;
  setRecordingTransport({
    uploadRecordingChunk: async () => ({ status: "received" }),
    fetchUploadStatus: async () => liveStatus([0], "pending"),
    finalizeRecording: async () => {
      finalizeCalls += 1;
      throw new Error("still offline");
    },
  });

  await pumpSession(sessionId);
  assert.equal(finalizeCalls, 1);
  assert.equal(recordingView().error, "結束標記送出失敗：still offline");
  const during = await load(sessionId);
  assert.equal(during?.finalize_persisted_at, persistedAt);
  assert.equal(during?.next_sequence, 1);
  assert.deepEqual(during?.finalize_payload, {
    final_sequence: 0,
    total_chunks: 1,
    total_duration_ms: 1_000,
  });

  await releaseChunk(sessionId, 0);
  await pumpSession(sessionId);

  assert.equal(finalizeCalls, 1);
  assert.equal(recordingView().error, "結束標記送出失敗：still offline");
  assert.equal(recordingView().uploadedCount, 1);
  assert.equal(recordingView().pendingCount, 0);
  const after = await load(sessionId);
  assert.equal(after?.finalize_persisted_at, persistedAt);
  assert.equal(after?.next_sequence, 1);
  assert.deepEqual(after?.finalize_payload, during?.finalize_payload);
  assert.equal(after?.recorder_state, "stopped");
});

test("a confirmed chunk keeps another transient failure visible", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-r4a";
  await seed(sessionId, { ...emptyMeta(sessionId, 1_000), next_sequence: 2, last_chunk_end_ms: 2_000 });
  await seedChunk(sessionId, 0, "first");
  await seedChunk(sessionId, 1, "second");
  const acked = new Set<number>();
  const attempts = new Map<number, number>();
  let finalized = false;
  setRecordingTransport({
    uploadRecordingChunk: async (_sessionId, sequence) => {
      const seen = (attempts.get(sequence) ?? 0) + 1;
      attempts.set(sequence, seen);
      if (seen === 1) {
        throw new TypeError("Failed to fetch");
      }
      acked.add(sequence);
      return { status: "received" };
    },
    fetchUploadStatus: async () => liveStatus([...acked]),
    finalizeRecording: async () => {
      finalized = true;
      throw new Error("finalize must not run");
    },
  });

  await pumpSession(sessionId);
  assert.equal(recordingView().error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  await pumpSession(sessionId);
  assert.equal(recordingView().error, "片段 1 上傳失敗：TypeError: Failed to fetch");

  const database = await openRecordingDb();
  const parked = await listChunks(database, sessionId);
  const chunk0 = parked.find((chunk) => chunk.sequence === 0);
  const chunk1 = parked.find((chunk) => chunk.sequence === 1);
  if (!chunk0 || !chunk1) {
    throw new Error("both failed slices should still be queued");
  }
  await writeChunk(database, { ...chunk1, next_attempt_at: 0 });
  await writeChunk(database, { ...chunk0, next_attempt_at: Date.now() + 60_000 });
  database.close();

  await pumpSession(sessionId);
  const during = recordingView();
  assert.equal(during.uploadedCount, 1);
  assert.equal(during.failedCount, 1);
  assert.equal(during.pendingCount, 1);
  assert.equal(during.error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  assert.equal(finalized, false);
  const mid = await load(sessionId);
  assert.equal(mid?.next_sequence, 2);
  assert.equal(mid?.finalize_payload, null);
  assert.equal(mid?.finalize_persisted_at, null);
  assert.equal(mid?.recorder_state, "recording");

  await releaseChunk(sessionId, 0);
  await pumpSession(sessionId);
  const after = recordingView();
  assert.equal(after.uploadedCount, 2);
  assert.equal(after.failedCount, 0);
  assert.equal(after.pendingCount, 0);
  assert.equal(after.error, null);
  assert.equal(finalized, false);
  const meta = await load(sessionId);
  assert.equal(meta?.next_sequence, 2);
  assert.equal(meta?.finalize_payload, null);
  assert.equal(meta?.finalize_persisted_at, null);
});

test("a later status listing an acked slice clears that transient failure", async () => {
  await resetRecordingControllerForTests();
  const sessionId = "S-r4b";
  await seed(sessionId, { ...emptyMeta(sessionId, 1_000), next_sequence: 2, last_chunk_end_ms: 2_000 });
  await seedChunk(sessionId, 0, "first");
  await seedChunk(sessionId, 1, "second", Date.now() + 60_000);
  let uploads = 0;
  let statusReads = 0;
  let finalized = false;
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      uploads += 1;
      if (uploads === 1) {
        throw new TypeError("Failed to fetch");
      }
      return { status: "received" };
    },
    fetchUploadStatus: async () => {
      statusReads += 1;
      if (statusReads === 1) {
        throw new TypeError("Failed to fetch");
      }
      return liveStatus([0, 1]);
    },
    finalizeRecording: async () => {
      finalized = true;
      throw new Error("finalize must not run");
    },
  });

  await pumpSession(sessionId);
  assert.equal(recordingView().error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  await releaseChunk(sessionId, 0);
  await pumpSession(sessionId);
  assert.equal(recordingView().uploadedCount, 1);
  assert.equal(recordingView().error, "片段 0 上傳失敗：TypeError: Failed to fetch");
  const mid = await load(sessionId);
  assert.equal(mid?.next_sequence, 2);
  assert.equal(mid?.finalize_payload, null);
  assert.equal(mid?.finalize_persisted_at, null);

  await releaseChunk(sessionId, 1);
  await pumpSession(sessionId);
  const after = recordingView();
  assert.equal(after.uploadedCount, 2);
  assert.equal(after.failedCount, 0);
  assert.equal(after.pendingCount, 0);
  assert.equal(after.error, null);
  assert.equal(finalized, false);
  const meta = await load(sessionId);
  assert.equal(meta?.next_sequence, 2);
  assert.equal(meta?.finalize_payload, null);
  assert.equal(meta?.finalize_persisted_at, null);
  assert.equal(meta?.recorder_state, "recording");
});
