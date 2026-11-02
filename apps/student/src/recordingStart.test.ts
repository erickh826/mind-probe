// Starting a take: the admission gate, the D3 claim, the narrowed same-id
// retry, and the takeover stop (D4). The camera and MediaRecorder are fakes;
// IndexedDB is fake-indexeddb.
import "fake-indexeddb/auto";

import assert from "node:assert/strict";
import { afterEach } from "node:test";
import test from "node:test";
import { ApiError } from "./apiError.ts";
import {
  bindSession,
  notePageHide,
  pumpSession,
  recorderStopPromiseForTests,
  recordingView,
  resetRecordingControllerForTests,
  setRecordingTransport,
  setSocketAdmitted,
  startRecording,
  stopReplacedRecording,
} from "./recordingController.ts";
import { listChunks, openRecordingDb, readMeta } from "./recordingDb.ts";
import type { RecordingMeta, ServerUploadStatus } from "./uploadQueue.ts";

interface FakeTrack {
  stopped: boolean;
  stop: () => void;
}

class FakeRecorder {
  static instances: FakeRecorder[] = [];
  static startBehaviour: (recorder: FakeRecorder) => void = () => {};
  static isTypeSupported(): boolean {
    return true;
  }
  state = "inactive";
  starts = 0;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeRecorder.instances.push(this);
  }
  start(): void {
    this.starts += 1;
    FakeRecorder.startBehaviour(this);
    this.state = "recording";
  }
  stop(): void {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["last"], { type: "video/webm" }) });
    this.onstop?.();
  }
}

interface Env {
  prompts: number;
  tracks: FakeTrack[];
  claims: string[];
  statusCalls: number;
  status: ServerUploadStatus;
  /** Override one claim answer; return undefined to accept. */
  claim: (takeId: string) => Promise<unknown>;
  prompt: () => Promise<void>;
  uploads: number[];
  finalizes: Array<{ final_sequence: number; reason?: string | null }>;
}

const free = (): ServerUploadStatus => ({
  received_sequences: [],
  finalize_state: "none",
  recording_state: "recording",
  take_claimed: false,
});

let restoreGlobals: (() => void) | null = null;

function install(): Env {
  const env: Env = {
    prompts: 0,
    tracks: [],
    claims: [],
    statusCalls: 0,
    status: free(),
    claim: async () => ({ take_id: "ok" }),
    prompt: async () => {},
    uploads: [],
    finalizes: [],
  };
  FakeRecorder.instances = [];
  FakeRecorder.startBehaviour = () => {};
  const globals = globalThis as unknown as { MediaRecorder?: unknown };
  const hadRecorder = "MediaRecorder" in globals;
  const previousRecorder = globals.MediaRecorder;
  const previousDevices = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  globals.MediaRecorder = FakeRecorder;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: async () => {
        env.prompts += 1;
        await env.prompt();
        const track: FakeTrack = {
          stopped: false,
          stop() {
            this.stopped = true;
          },
        };
        env.tracks.push(track);
        return { getTracks: () => [track] };
      },
    },
  });
  restoreGlobals = () => {
    if (hadRecorder) {
      globals.MediaRecorder = previousRecorder;
    } else {
      delete globals.MediaRecorder;
    }
    if (previousDevices) {
      Object.defineProperty(navigator, "mediaDevices", previousDevices);
    } else {
      delete (navigator as unknown as { mediaDevices?: unknown }).mediaDevices;
    }
  };
  setRecordingTransport({
    fetchUploadStatus: async () => {
      env.statusCalls += 1;
      return env.status;
    },
    claimRecording: async (_sessionId, takeId) => {
      env.claims.push(takeId);
      return env.claim(takeId);
    },
    uploadRecordingChunk: async (_sessionId, sequence) => {
      env.uploads.push(sequence);
      return { status: "received" };
    },
    finalizeRecording: async (_sessionId, payload) => {
      env.finalizes.push({ final_sequence: payload.final_sequence, reason: payload.reason });
      return {
        recording_state: "incomplete",
        finalize_state: "incomplete",
        final_sequence: payload.final_sequence,
        missing_sequences: [],
      };
    },
  });
  return env;
}

afterEach(async () => {
  restoreGlobals?.();
  restoreGlobals = null;
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

function conflict(code: string): ApiError {
  return new ApiError(409, code, code);
}

async function admitted(sessionId: string): Promise<void> {
  await bindSession(sessionId);
  setSocketAdmitted(sessionId, true);
}

test("no camera prompt and no claim until the socket is admitted", async () => {
  const env = install();
  const sessionId = "S-not-admitted";
  await bindSession(sessionId);
  await startRecording(sessionId);
  assert.equal(env.prompts, 0);
  assert.deepEqual(env.claims, []);
  assert.match(recordingView().error ?? "", /尚未連上場次/);
  assert.equal(await load(sessionId), null);
});

test("an admitted fresh tab claims once after the camera and records", async () => {
  const env = install();
  const sessionId = "S-happy";
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(env.prompts, 1);
  assert.equal(env.claims.length, 1);
  assert.equal(FakeRecorder.instances.length, 1);
  assert.equal(FakeRecorder.instances[0].state, "recording");
  assert.equal(recordingView().status, "recording");
  assert.equal((await load(sessionId))?.recorder_state, "recording");
});

test("a session whose take exists elsewhere never opens the camera", async () => {
  for (const status of [
    { ...free(), take_claimed: true },
    { ...free(), received_sequences: [0] },
    { ...free(), finalize_state: "pending" as const },
  ]) {
    await resetRecordingControllerForTests();
    const env = install();
    env.status = status;
    const sessionId = "S-claimed-elsewhere";
    await admitted(sessionId);
    await startRecording(sessionId);
    assert.equal(env.prompts, 0);
    assert.deepEqual(env.claims, []);
    const shown = recordingView();
    assert.equal(shown.canStart, false);
    assert.equal(shown.takeCheck, "claimed");
    assert.match(shown.error ?? "", /此場次已有錄影/);
    restoreGlobals?.();
    restoreGlobals = null;
  }
});

test("a failed status check does not start, and the next admission asks again", async () => {
  const env = install();
  const sessionId = "S-status-down";
  setRecordingTransport({
    fetchUploadStatus: async () => {
      env.statusCalls += 1;
      throw new ApiError(0, "unreachable");
    },
  });
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(env.prompts, 0);
  assert.equal(recordingView().takeCheck, "unknown");
  assert.match(recordingView().error ?? "", /無法確認/);

  const before = env.statusCalls;
  setSocketAdmitted(sessionId, false);
  setSocketAdmitted(sessionId, true);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(env.statusCalls > before, "re-admission retries the check");
});

test("double click, auto-start and the camera retry share one attempt", async () => {
  const env = install();
  const sessionId = "S-concurrent";
  const releases: Array<() => void> = [];
  env.prompt = () => new Promise<void>((resolve) => releases.push(resolve));
  await admitted(sessionId);
  const first = startRecording(sessionId);
  const second = startRecording(sessionId);
  const third = startRecording(sessionId);
  await new Promise((resolve) => setTimeout(resolve, 10));
  releases.forEach((release) => release());
  await Promise.all([first, second, third]);
  assert.equal(env.prompts, 1);
  assert.equal(env.claims.length, 1);
  assert.equal(FakeRecorder.instances.length, 1);
  await startRecording(sessionId);
  assert.equal(env.prompts, 1, "a later start sees the take and does nothing");
});

test("losing admission during the camera prompt releases the camera without claiming", async () => {
  const env = install();
  const sessionId = "S-lost-in-prompt";
  let release: () => void = () => {};
  env.prompt = () => new Promise<void>((resolve) => (release = resolve));
  await admitted(sessionId);
  const starting = startRecording(sessionId);
  await new Promise((resolve) => setTimeout(resolve, 10));
  setSocketAdmitted(sessionId, false);
  release();
  await starting;
  assert.deepEqual(env.claims, []);
  assert.equal(env.tracks[0].stopped, true);
  assert.equal(FakeRecorder.instances.length, 0);
});

test("claim refusals each have their own outcome", async () => {
  const cases: Array<{
    error: ApiError;
    message: RegExp;
    canStart: boolean;
  }> = [
    { error: conflict("recording_already_claimed"), message: /此場次已有錄影/, canStart: false },
    { error: conflict("session_ended"), message: /場次已結束/, canStart: false },
    { error: new ApiError(401, "revoked_session_token", "revoked_session_token"), message: /已被撤銷/, canStart: false },
    { error: new ApiError(401, "expired_session_token", "expired_session_token"), message: /登入已失效/, canStart: false },
    { error: conflict("role_not_admitted"), message: /尚未連上場次/, canStart: true },
  ];
  for (const { error, message, canStart } of cases) {
    await resetRecordingControllerForTests();
    const env = install();
    env.claim = async () => {
      throw error;
    };
    const sessionId = `S-claim-${error.code}`;
    await admitted(sessionId);
    await startRecording(sessionId);
    assert.equal(env.tracks[0].stopped, true, `${error.code}: camera released`);
    assert.equal(FakeRecorder.instances.length, 0, `${error.code}: no recorder`);
    assert.equal(await load(sessionId), null, `${error.code}: no take written`);
    assert.match(recordingView().error ?? "", message, String(error.code));
    assert.equal(recordingView().canStart, canStart, String(error.code));
    restoreGlobals?.();
    restoreGlobals = null;
  }
});

test("after another device's claim wins, pressing again asks the server and stays locked", async () => {
  const env = install();
  const sessionId = "S-lost-race";
  env.claim = async () => {
    throw conflict("recording_already_claimed");
  };
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(env.claims.length, 1);

  env.status = { ...free(), take_claimed: true };
  await startRecording(sessionId);
  assert.equal(env.prompts, 1, "no second camera prompt");
  assert.equal(env.claims.length, 1, "the losing id is not retried");
  assert.equal(recordingView().canStart, false);
});

test("a pending claim is confirmed with the same id and skips only the claimed check", async () => {
  const env = install();
  const sessionId = "S-pending";
  env.claim = async () => {
    throw conflict("role_not_admitted");
  };
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(env.claims.length, 1);

  // The server now reports the take as claimed -- by this tab's own id.
  env.status = { ...free(), take_claimed: true };
  env.claim = async () => ({ take_id: env.claims[0] });
  await startRecording(sessionId);
  assert.equal(env.claims.length, 2);
  assert.equal(env.claims[1], env.claims[0], "same take id, never a new one");
  assert.equal(FakeRecorder.instances.length, 1);
  assert.equal(recordingView().status, "recording");
});

test("a lost claim answer is retried with the same id, then kept for the next press", async () => {
  const env = install();
  const sessionId = "S-claim-network";
  env.claim = async () => {
    throw new ApiError(0, "unreachable");
  };
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(env.claims.length, 3, "bounded retries");
  assert.equal(new Set(env.claims).size, 1);
  assert.match(recordingView().error ?? "", /再按一次/);
  assert.equal(env.tracks[0].stopped, true);

  // Retries ran out but the claim had landed: the next press confirms it.
  env.status = { ...free(), take_claimed: true };
  env.claim = async () => ({ take_id: env.claims[0] });
  await startRecording(sessionId);
  assert.equal(env.claims[3], env.claims[0]);
  assert.equal(recordingView().status, "recording");
});

test("a server without the claim route still records", async () => {
  const env = install();
  const sessionId = "S-legacy";
  env.claim = async () => {
    throw new ApiError(404, "Not Found", "Not Found");
  };
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(recordingView().status, "recording");
});

test("MediaRecorder.start() throwing never captured: same tab retries the same id", async () => {
  const env = install();
  const sessionId = "S-start-throws";
  FakeRecorder.startBehaviour = () => {
    throw new Error("NotSupportedError");
  };
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(recordingView().status, "error");
  assert.equal(env.tracks[0].stopped, true);
  assert.equal(await load(sessionId), null, "only the attempt's own meta is removed");
  assert.equal(recordingView().canStart, true);

  FakeRecorder.startBehaviour = () => {};
  await startRecording(sessionId);
  assert.equal(env.claims.length, 2);
  assert.equal(env.claims[1], env.claims[0]);
  assert.equal(recordingView().status, "recording");
});

test("a slice stored before start() threw means capture happened: lost, not restarted", async () => {
  const env = install();
  const sessionId = "S-start-raced";
  FakeRecorder.startBehaviour = (recorder) => {
    // A first slice arrives (its IndexedDB write still pending) and then start fails.
    recorder.ondataavailable?.({ data: new Blob(["first"], { type: "video/webm" }) });
    throw new Error("late failure");
  };
  await admitted(sessionId);
  await startRecording(sessionId);
  const meta = await load(sessionId);
  assert.equal(meta?.next_sequence, 1, "the racing slice was kept");
  assert.equal(meta?.recorder_state, "lost");
  assert.equal(meta?.finalize_payload?.reason, "recorder_lost");
  assert.equal(recordingView().canStart, false);

  FakeRecorder.startBehaviour = () => {};
  await startRecording(sessionId);
  assert.equal(env.claims.length, 1, "never claimed or started again");
  assert.equal(FakeRecorder.instances.length, 1);
});

test("a take that started and was lost is never restarted, even with no chunk", async () => {
  const env = install();
  const sessionId = "S-started-then-lost";
  await admitted(sessionId);
  await startRecording(sessionId);
  // pagehide before any slice was stored
  FakeRecorder.instances[0].ondataavailable = null;
  notePageHide();
  await recorderStopPromiseForTests();
  assert.equal((await load(sessionId))?.recorder_state, "lost");
  await startRecording(sessionId);
  assert.equal(env.claims.length, 1);
  assert.equal(env.prompts, 1);
});

test("a replaced tab stops as recorder_lost and keeps uploading with its live token", async () => {
  const env = install();
  const sessionId = "S-replaced";
  await admitted(sessionId);
  await startRecording(sessionId);
  const recorder = FakeRecorder.instances[0];
  recorder.ondataavailable?.({ data: new Blob(["first"], { type: "video/webm" }) });

  setSocketAdmitted(sessionId, false);
  await stopReplacedRecording(sessionId);
  await recorderStopPromiseForTests();

  assert.equal(recorder.state, "inactive");
  assert.equal(env.tracks[0].stopped, true);
  const meta = await load(sessionId);
  assert.equal(meta?.recorder_state, "lost");
  assert.equal(meta?.next_sequence, 2);
  assert.equal(meta?.final_sequence, 1, "the slice flushed on stop is declared");
  assert.equal(meta?.finalize_payload?.reason, "recorder_lost");
  assert.equal(meta?.upload_block, null);
  assert.equal(meta?.needs_operator, false);
  assert.match(recordingView().error ?? "", /已被另一個連線取代/);
  assert.equal(recordingView().canStart, false);

  const database = await openRecordingDb();
  try {
    const chunks = await listChunks(database, sessionId);
    assert.ok(chunks.every((chunk) => !chunk.terminal), "nothing marked unauthorized");
  } finally {
    database.close();
  }
  for (let i = 0; i < 4; i += 1) {
    await pumpSession(sessionId);
  }
  assert.deepEqual([...env.uploads].sort(), [0, 1]);
  assert.deepEqual(env.finalizes, [{ final_sequence: 1, reason: "recorder_lost" }]);

  setSocketAdmitted(sessionId, true);
  await startRecording(sessionId);
  assert.equal(env.prompts, 1, "the replaced tab never records again");
});

async function holdLockElsewhere(sessionId: string): Promise<() => void> {
  let release: () => void = () => {};
  const held = new Promise<void>((granted) => {
    void navigator.locks.request(`mindprobe-recorder:${sessionId}`, () => {
      granted();
      return new Promise<void>((done) => (release = done));
    });
  });
  await held;
  return () => release();
}

async function heldLocks(): Promise<string[]> {
  const snapshot = await navigator.locks.query();
  return (snapshot.held ?? []).map((lock) => lock.name ?? "");
}

test("a duplicated tab does not declare another tab's live take lost", async () => {
  install();
  const sessionId = "S-dup-tab";
  const database = await openRecordingDb();
  try {
    const { emptyMeta } = await import("./uploadQueue.ts");
    const { writeMeta } = await import("./recordingDb.ts");
    await writeMeta(database, emptyMeta(sessionId, 1));
  } finally {
    database.close();
  }
  const release = await holdLockElsewhere(sessionId);
  await bindSession(sessionId);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal((await load(sessionId))?.recorder_state, "recording", "still the other tab's live take");
  assert.match(recordingView().error ?? "", /另一個分頁錄影/);
  assert.equal(recordingView().canStart, false);

  // The other tab is gone without a stop: now the take really is lost.
  release();
  await new Promise((resolve) => setTimeout(resolve, 3_300));
  assert.equal((await load(sessionId))?.recorder_state, "lost");
});

test("a start while another tab holds the take never writes or records", async () => {
  const env = install();
  const sessionId = "S-dup-start";
  const release = await holdLockElsewhere(sessionId);
  try {
    await admitted(sessionId);
    await startRecording(sessionId);
    assert.equal(FakeRecorder.instances.length, 0);
    assert.equal(env.tracks[0].stopped, true);
    assert.equal(await load(sessionId), null);
    assert.match(recordingView().error ?? "", /另一個分頁錄影/);
  } finally {
    release();
  }
});

test("the recording tab holds the lock until its take is declared", async () => {
  install();
  const sessionId = "S-lock-held";
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.ok((await heldLocks()).includes(`mindprobe-recorder:${sessionId}`));
  FakeRecorder.instances[0].stop();
  await recorderStopPromiseForTests();
  assert.ok(!(await heldLocks()).includes(`mindprobe-recorder:${sessionId}`));
  assert.equal((await load(sessionId))?.recorder_state, "stopped");
});
