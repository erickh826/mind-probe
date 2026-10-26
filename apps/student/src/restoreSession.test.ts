import "fake-indexeddb/auto";

import assert from "node:assert/strict";
import { afterEach } from "node:test";
import test from "node:test";
import { ApiError } from "./apiError.ts";
import {
  bindSession,
  persistBlob,
  pumpSession,
  resetRecordingControllerForTests,
  setRecordingTransport,
} from "./recordingController.ts";
import { openRecordingDb, readMeta, writeMeta } from "./recordingDb.ts";
import {
  restoreStudentSession,
  UNAVAILABLE_RESTORE_MESSAGE,
  type RestoreProbes,
} from "./restoreSession.ts";
import { emptyMeta, persistStop, type RecordingMeta } from "./uploadQueue.ts";

const stored = {
  sessionId: "S-ended",
  role: "student",
  accessToken: "student-token",
};

function probes(partial: Partial<RestoreProbes> & Pick<RestoreProbes, "fetchSession" | "fetchUploadStatus">): {
  probes: RestoreProbes;
  cleared: string[];
  clipCalls: number;
} {
  const cleared: string[] = [];
  let clipCalls = 0;
  return {
    cleared,
    get clipCalls() {
      return clipCalls;
    },
    probes: {
      fetchClipManifest: async () => {
        clipCalls += 1;
        return [];
      },
      clearJoinedSession: (sessionId: string) => {
        cleared.push(sessionId);
      },
      ...partial,
    },
  };
}

async function seed(sessionId: string, meta: RecordingMeta): Promise<void> {
  const database = await openRecordingDb();
  try {
    await writeMeta(database, meta);
  } finally {
    database.close();
  }
}

afterEach(async () => {
  await resetRecordingControllerForTests();
});

async function load(sessionId: string): Promise<RecordingMeta | null> {
  const database = await openRecordingDb();
  try {
    return await readMeta(database, sessionId);
  } finally {
    database.close();
  }
}

test("after /end, reload restores upload-only mode without session or clip access", async () => {
  let clipCalls = 0;
  const cleared: string[] = [];
  const restored = await restoreStudentSession(stored, {
    fetchSession: async () => {
      throw new ApiError(401, "revoked", "revoked_session_token");
    },
    fetchClipManifest: async () => {
      clipCalls += 1;
      throw new ApiError(401, "revoked", "revoked_session_token");
    },
    fetchUploadStatus: async () => ({
      finalize_state: "pending",
      recording_state: "finalizing",
      received_sequences: [0],
    }),
    clearJoinedSession: (sessionId) => {
      cleared.push(sessionId);
    },
  });

  assert.equal(restored.kind, "upload-only");
  assert.equal(clipCalls, 0);
  assert.deepEqual(cleared, []);
  if (restored.kind !== "upload-only") {
    return;
  }
  assert.equal(restored.sessionId, stored.sessionId);
  assert.equal(restored.accessToken, stored.accessToken);

  await resetRecordingControllerForTests();
  await seed(stored.sessionId, emptyMeta(stored.sessionId, 0));
  await persistBlob(stored.sessionId, new Blob(["chunk"], { type: "video/webm" }));
  const queued = await load(stored.sessionId);
  assert.ok(queued);
  await seed(stored.sessionId, persistStop(queued, Date.now() - 31_000));

  let uploaded = 0;
  let finalized = 0;
  setRecordingTransport({
    uploadRecordingChunk: async () => {
      uploaded += 1;
      return { status: "received" };
    },
    fetchUploadStatus: async () => ({
      finalize_state: "pending",
      recording_state: "finalizing",
      // The seeded chunk is still local. Claiming it here would delete it
      // before the upload, which is not the pending-chunk case.
      received_sequences: [],
    }),
    finalizeRecording: async () => {
      finalized += 1;
      return {
        recording_state: "complete",
        finalize_state: "complete",
        final_sequence: 0,
        missing_sequences: [],
      };
    },
  });
  await bindSession(stored.sessionId);
  await pumpSession(stored.sessionId);
  assert.equal(uploaded, 1);
  assert.equal(finalized, 1);
  const done = await load(stored.sessionId);
  assert.equal(done?.finalize_confirmed, true);
  assert.equal(done?.server_finalize_state, "complete");
});

test("a confirmed take still restores after /end", async () => {
  const restored = await restoreStudentSession(stored, {
    fetchSession: async () => {
      throw new ApiError(401, "revoked", "revoked_session_token");
    },
    fetchClipManifest: async () => {
      throw new Error("clips must not be required");
    },
    fetchUploadStatus: async () => ({ finalize_state: "complete" }),
    clearJoinedSession: () => {
      throw new Error("credentials must stay");
    },
  });
  assert.equal(restored.kind, "upload-only");
});

test("grace expiry and security revocation both drop the saved credential", async () => {
  for (const label of ["grace-expired", "security-revocation"]) {
    const sink = probes({
      fetchSession: async () => {
        throw new ApiError(401, "revoked", "revoked_session_token");
      },
      fetchUploadStatus: async () => {
        throw new ApiError(401, label, "revoked_session_token");
      },
    });
    const restored = await restoreStudentSession(stored, sink.probes);
    assert.equal(restored.kind, "rejected");
    assert.deepEqual(sink.cleared, [stored.sessionId]);
    assert.equal(sink.clipCalls, 0);
  }
});

test("a live reload does not become upload-only when the session read fails open", async () => {
  for (const cause of [new TypeError("network down"), new ApiError(503, "unavailable", null)]) {
    let statusCalls = 0;
    const sink = probes({
      fetchSession: async () => {
        throw cause;
      },
      fetchUploadStatus: async () => {
        statusCalls += 1;
        return { finalize_state: "pending", recording_state: "recording", received_sequences: [] };
      },
    });
    const restored = await restoreStudentSession(stored, sink.probes);
    assert.equal(restored.kind, "unavailable");
    assert.equal(statusCalls, 0);
    assert.equal(sink.clipCalls, 0);
    assert.deepEqual(sink.cleared, []);
  }

  let statusCalls = 0;
  const clipsDown = probes({
    fetchSession: async () => ({ session_id: stored.sessionId, case_id: "CASE001" }),
    fetchUploadStatus: async () => {
      statusCalls += 1;
      return { finalize_state: "pending" };
    },
  });
  clipsDown.probes.fetchClipManifest = async () => {
    throw new ApiError(503, "unavailable", null);
  };
  const restored = await restoreStudentSession(stored, clipsDown.probes);
  assert.equal(restored.kind, "unavailable");
  assert.equal(statusCalls, 0);
  assert.deepEqual(clipsDown.cleared, []);
  assert.equal(
    UNAVAILABLE_RESTORE_MESSAGE,
    "Could not reach the saved session. Check the network and try again.",
  );
});

test("a live session still loads clips and does not enter upload-only mode", async () => {
  const sink = probes({
    fetchSession: async () => ({ session_id: stored.sessionId, case_id: "CASE001" }),
    fetchUploadStatus: async () => {
      throw new Error("status is not the live path");
    },
  });
  sink.probes.fetchClipManifest = async () => {
    return [
      {
        clip_id: "idle",
        category: "IDLE",
        intent: "idle",
        character_state: "neutral",
        text: "",
        reveals: [],
        requires: [],
        forbidden_after: [],
        duration_ms: 0,
        file: "idle.mp4",
        hotkey: null,
      },
    ];
  };
  const restored = await restoreStudentSession(stored, sink.probes);
  assert.equal(restored.kind, "live");
  if (restored.kind === "live") {
    assert.equal(restored.caseId, "CASE001");
    assert.equal(restored.clips.length, 1);
  }
  assert.deepEqual(sink.cleared, []);
});
