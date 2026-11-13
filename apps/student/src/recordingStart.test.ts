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
  interruptRecording,
  notePageHide,
  openCameraPreview,
  pumpSession,
  recorderStopPromiseForTests,
  recordingView,
  resetRecordingControllerForTests,
  releaseLocalMedia,
  setRecordingArmed,
  setRecordingTransport,
  setSocketAdmitted,
  setStorageCheckForTests,
  startRecording,
  stopReplacedRecording,
} from "./recordingController.ts";
import { listChunks, openRecordingDb, readMeta } from "./recordingDb.ts";
import type { RecordingMeta, ServerUploadStatus } from "./uploadQueue.ts";

interface FakeTrack {
  stopped: boolean;
  readyState: string;
  stop: () => void;
  addEventListener: (type: string, listener: () => void) => void;
  end: () => void;
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
  /** Slices the server refused because a finalize already ended before them. */
  pastFinal: number[];
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
    pastFinal: [],
  };
  // Like the server: the first finalize fixes the take's last sequence.
  let serverFinal: number | null = null;
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
        const listeners = new Set<() => void>();
        const track: FakeTrack = {
          readyState: "live",
          stopped: false,
          stop() {
            this.stopped = true;
            this.readyState = "ended";
          },
          addEventListener(_type: string, listener: () => void) {
            listeners.add(listener);
          },
          end() {
            this.readyState = "ended";
            for (const listener of listeners) {
              listener();
            }
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
      if (serverFinal !== null && sequence > serverFinal) {
        env.pastFinal.push(sequence);
        throw conflict("sequence_past_final");
      }
      env.uploads.push(sequence);
      return { status: "received" };
    },
    finalizeRecording: async (_sessionId, payload) => {
      serverFinal ??= payload.final_sequence;
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

test("recording does not start until the server session is armed", async () => {
  const env = install();
  const sessionId = "S-not-armed";
  await admitted(sessionId);
  setRecordingArmed(sessionId, false);
  await startRecording(sessionId);
  assert.equal(env.prompts, 0);
  assert.deepEqual(env.claims, []);
  assert.equal(FakeRecorder.instances.length, 0);

  setRecordingArmed(sessionId, true);
  await startRecording(sessionId);
  assert.equal(env.claims.length, 1);
  assert.equal(FakeRecorder.instances.length, 1);
  assert.equal(FakeRecorder.instances[0].state, "recording");
});

test("a failed storage self-check blocks the claim", async () => {
  const env = install();
  const sessionId = "S-idb-blocked";
  setStorageCheckForTests(async () => ({
    phase: "start",
    step: "open_db",
    errorName: "QuotaExceededError",
    message: "此裝置的儲存空間不足（QuotaExceededError，開啟資料庫）。不要清除網站資料。",
  }));
  await admitted(sessionId);
  await startRecording(sessionId);
  assert.equal(env.prompts, 0);
  assert.deepEqual(env.claims, []);
  assert.equal(recordingView().canStart, false);
  assert.equal(recordingView().problem?.errorName, "QuotaExceededError");
  assert.equal(recordingView().problem?.step, "open_db");
  assert.match(recordingView().error ?? "", /不要清除網站資料/);
});

test("leaving an in-progress take keeps stored chunks and does not delete the database", async () => {
  install();
  const sessionId = "S-leave";
  await admitted(sessionId);
  await startRecording(sessionId);
  const deleted: string[] = [];
  const original = indexedDB.deleteDatabase.bind(indexedDB);
  indexedDB.deleteDatabase = ((name: string) => {
    deleted.push(name);
    return original(name);
  }) as typeof indexedDB.deleteDatabase;
  try {
    releaseLocalMedia();
    await recorderStopPromiseForTests();
  } finally {
    indexedDB.deleteDatabase = original;
  }
  assert.equal(deleted.includes("mindprobe-recording"), false);
  const meta = await load(sessionId);
  assert.equal(meta?.recorder_state, "lost");
  assert.ok((meta?.next_sequence ?? 0) >= 1);
  const rows = await chunkRows(sessionId);
  assert.ok(rows.length >= 1);
});

test("a preview stream that resolves after leave is stopped immediately", async () => {
  const env = install();
  let releasePrompt: () => void = () => {};
  env.prompt = () =>
    new Promise<void>((resolve) => {
      releasePrompt = resolve;
    });
  const sessionId = "S-late-preview";
  await bindSession(sessionId);
  const opening = openCameraPreview(sessionId);
  await waitFor(async () => env.prompts === 1, "camera prompt");
  releaseLocalMedia();
  releasePrompt();
  await opening;
  assert.equal(env.tracks.length, 1);
  assert.equal(env.tracks[0].stopped, true);
  assert.equal(recordingView().stream, null);
});

test("an ended preview track tears the preview down", async () => {
  const env = install();
  const sessionId = "S-track-ended";
  await bindSession(sessionId);
  await openCameraPreview(sessionId);
  assert.equal(recordingView().stream === null, false);
  env.tracks[0].end();
  assert.equal(env.tracks[0].stopped, true);
  assert.equal(recordingView().stream, null);
  assert.match(recordingView().error ?? "", /預覽已停止/);
});

test("revoking camera permission stops an open preview", async () => {
  const env = install();
  let state: PermissionState = "granted";
  const listeners = new Set<() => void>();
  const previous = Object.getOwnPropertyDescriptor(navigator, "permissions");
  Object.defineProperty(navigator, "permissions", {
    configurable: true,
    value: {
      query: async () => ({
        get state() {
          return state;
        },
        addEventListener(_type: string, listener: () => void) {
          listeners.add(listener);
        },
        removeEventListener(_type: string, listener: () => void) {
          listeners.delete(listener);
        },
      }),
    },
  });
  try {
    const sessionId = "S-permission";
    await bindSession(sessionId);
    await openCameraPreview(sessionId);
    assert.equal(env.tracks[0].stopped, false);
    state = "denied";
    for (const listener of listeners) {
      listener();
    }
    assert.equal(env.tracks[0].stopped, true);
    assert.equal(recordingView().stream, null);
    assert.match(recordingView().error ?? "", /權限已關閉/);
  } finally {
    if (previous) {
      Object.defineProperty(navigator, "permissions", previous);
    } else {
      delete (navigator as unknown as { permissions?: unknown }).permissions;
    }
  }
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
    { error: conflict("session_not_started"), message: /尚未將場次標為進行中/, canStart: true },
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

async function waitFor(check: () => Promise<boolean>, what: string, timeoutMs = 2_000): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await check()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail(`timed out waiting: ${what}`);
}

/**
 * Hold every slice's IndexedDB write: persisting a slice hashes it first.
 * Released by the returned function; the hold is removed in either case.
 */
function holdSliceWrites(): () => void {
  const subtle = crypto.subtle as unknown as { digest: SubtleCrypto["digest"] };
  const digest = subtle.digest.bind(crypto.subtle);
  let open: () => void = () => {};
  const gate = new Promise<void>((resolve) => (open = resolve));
  subtle.digest = (async (...args: Parameters<SubtleCrypto["digest"]>) => {
    await gate;
    return digest(...args);
  }) as SubtleCrypto["digest"];
  restoreDigest = () => {
    Reflect.deleteProperty(crypto.subtle, "digest");
    open();
  };
  return () => restoreDigest?.();
}

let restoreDigest: (() => void) | null = null;
afterEach(() => {
  restoreDigest?.();
  restoreDigest = null;
});

/** Make every attempt to store a slice whose bytes are `text` fail. */
function failSliceWrites(text: string): void {
  const subtle = crypto.subtle as unknown as { digest: SubtleCrypto["digest"] };
  const digest = subtle.digest.bind(crypto.subtle);
  subtle.digest = (async (algorithm: AlgorithmIdentifier, data: BufferSource) => {
    const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : new Uint8Array(data.buffer);
    if (new TextDecoder().decode(bytes) === text) {
      throw new Error("QuotaExceededError");
    }
    return digest(algorithm, data);
  }) as SubtleCrypto["digest"];
  restoreDigest = () => {
    Reflect.deleteProperty(crypto.subtle, "digest");
  };
}

/** Pump with the clock past the finalize deadline, as the 400 ms timer would later. */
async function pumpPastDeadline(sessionId: string, times: number): Promise<void> {
  const realNow = Date.now;
  Date.now = () => realNow() + 31_000;
  try {
    for (let i = 0; i < times; i += 1) {
      await pumpSession(sessionId);
    }
  } finally {
    Date.now = realNow;
  }
}

async function chunkRows(sessionId: string) {
  const database = await openRecordingDb();
  try {
    return await listChunks(database, sessionId);
  } finally {
    database.close();
  }
}

test("a takeover sends no finalize until the recorder's last slices are stored", async () => {
  const env = install();
  const sessionId = "S-replaced-flush";
  await admitted(sessionId);
  await startRecording(sessionId);
  const recorder = FakeRecorder.instances[0];
  recorder.ondataavailable?.({ data: new Blob(["s0"], { type: "video/webm" }) });
  recorder.ondataavailable?.({ data: new Blob(["s1"], { type: "video/webm" }) });
  await waitFor(async () => (await load(sessionId))?.next_sequence === 2, "slices 0 and 1 stored");
  for (let i = 0; i < 3; i += 1) {
    await pumpSession(sessionId);
  }
  assert.deepEqual([...env.uploads].sort(), [0, 1], "slices 0 and 1 are on the server");

  // Slice 2 is being written when the takeover arrives; stop() flushes slice 3
  // and onstop fires before either write settles.
  const releaseWrites = holdSliceWrites();
  recorder.ondataavailable?.({ data: new Blob(["s2"], { type: "video/webm" }) });
  setSocketAdmitted(sessionId, false);
  await stopReplacedRecording(sessionId);
  assert.equal(recorder.state, "inactive");
  await pumpPastDeadline(sessionId, 4);
  assert.deepEqual(env.finalizes, [], "no finalize while the last slices are being stored");
  const held = await load(sessionId);
  assert.equal(held?.recorder_state, "lost", "the lost marker is durable before the flush");
  assert.equal(held?.recorder_flush_pending, true);

  releaseWrites();
  await recorderStopPromiseForTests();
  const declared = await load(sessionId);
  assert.equal(declared?.recorder_flush_pending, false);
  assert.equal(declared?.next_sequence, 4);
  assert.equal(declared?.final_sequence, 3, "the declaration covers slices 0-3");
  for (let i = 0; i < 6; i += 1) {
    await pumpSession(sessionId);
  }
  assert.deepEqual([...env.uploads].sort(), [0, 1, 2, 3]);
  assert.deepEqual(env.pastFinal, [], "no slice refused as past the final sequence");
  assert.deepEqual(env.finalizes, [{ final_sequence: 3, reason: "recorder_lost" }]);
  const settled = await load(sessionId);
  assert.equal(settled?.server_finalize_state, "incomplete");
  assert.equal(settled?.upload_block, null);
  assert.deepEqual(await chunkRows(sessionId), [], "nothing stranded locally");
  assert.match(recordingView().error ?? "", /已被另一個連線取代/);
});

test("a held takeover declaration waits for the tab still storing it, and a later page releases it", async () => {
  const env = install();
  const sessionId = "S-replaced-reload";
  await admitted(sessionId);
  await startRecording(sessionId);
  const recorder = FakeRecorder.instances[0];
  recorder.ondataavailable?.({ data: new Blob(["s0"], { type: "video/webm" }) });
  await waitFor(async () => (await load(sessionId))?.next_sequence === 1, "slice 0 stored");
  // The page is interrupted right after stop(): onstop never runs here.
  recorder.stop = function stop(this: FakeRecorder) {
    this.state = "inactive";
  };
  await stopReplacedRecording(sessionId);
  await pumpPastDeadline(sessionId, 3);
  assert.deepEqual(env.finalizes, [], "the dying page never sends the held declaration");
  assert.equal((await load(sessionId))?.recorder_flush_pending, true);

  // A later page. While some tab still holds the recorder lock, that tab may
  // still be storing slices: leave the declaration held.
  await resetRecordingControllerForTests({ keepDatabase: true });
  restoreGlobals?.();
  const later = install();
  const release = await holdLockElsewhere(sessionId);
  await bindSession(sessionId);
  await new Promise((resolve) => setTimeout(resolve, 50));
  await pumpPastDeadline(sessionId, 3);
  assert.deepEqual(later.finalizes, []);
  assert.equal((await load(sessionId))?.recorder_flush_pending, true);

  // No recorder is left anywhere: declare what was stored and send it.
  release();
  await waitFor(async () => (await load(sessionId))?.recorder_flush_pending === false, "hold released", 6_000);
  for (let i = 0; i < 4; i += 1) {
    await pumpSession(sessionId);
  }
  assert.deepEqual([...env.uploads, ...later.uploads].sort(), [0]);
  assert.deepEqual(later.pastFinal, []);
  assert.deepEqual(later.finalizes, [{ final_sequence: 0, reason: "recorder_lost" }]);
  assert.equal((await load(sessionId))?.server_finalize_state, "incomplete");
});

test("a revocation's lost declaration also waits for the recorder's last slices", async () => {
  // The token is revoked, but an upload code may lift the block in this page:
  // whatever finalize goes out must cover the slices flushed on stop.
  const env = install();
  const sessionId = "S-revoked-flush";
  await admitted(sessionId);
  await startRecording(sessionId);
  const recorder = FakeRecorder.instances[0];
  recorder.ondataavailable?.({ data: new Blob(["s0"], { type: "video/webm" }) });
  await waitFor(async () => (await load(sessionId))?.next_sequence === 1, "slice 0 stored");

  const releaseWrites = holdSliceWrites();
  await interruptRecording(sessionId);
  await pumpPastDeadline(sessionId, 4);
  assert.deepEqual(env.finalizes, [], "no finalize while the last slice is being stored");
  assert.equal((await load(sessionId))?.recorder_flush_pending, true);

  releaseWrites();
  await recorderStopPromiseForTests();
  for (let i = 0; i < 6; i += 1) {
    await pumpSession(sessionId);
  }
  assert.deepEqual(env.pastFinal, []);
  assert.deepEqual(env.finalizes, [{ final_sequence: 1, reason: "recorder_lost" }]);
  assert.equal((await load(sessionId))?.needs_operator, true);
});

test("a slice that could not be stored stops the recorder and keeps earlier slices", async () => {
  const env = install();
  const sessionId = "S-chunk-lost";
  await admitted(sessionId);
  await startRecording(sessionId);
  const recorder = FakeRecorder.instances[0];
  recorder.ondataavailable?.({ data: new Blob(["s0"], { type: "video/webm" }) });
  await waitFor(async () => (await load(sessionId))?.next_sequence === 1, "slice 0 stored");

  failSliceWrites("bad");
  recorder.ondataavailable?.({ data: new Blob(["bad"], { type: "video/webm" }) });
  await waitFor(async () => recorder.state === "inactive", "recorder stopped after the write failure");
  await recorderStopPromiseForTests();
  assert.equal((await load(sessionId))?.recorder_state, "lost");
  assert.equal((await load(sessionId))?.next_sequence, 1, "the failed slice was not stored");
  assert.notEqual(recordingView().status, "recording");
  assert.match(recordingView().error ?? "", /不要清除網站資料|UnknownError|QuotaExceededError/);

  recorder.ondataavailable?.({ data: new Blob(["s1"], { type: "video/webm" }) });
  assert.equal((await load(sessionId))?.next_sequence, 1, "no further slice is appended");

  for (let i = 0; i < 6; i += 1) {
    await pumpSession(sessionId);
  }
  assert.deepEqual([...env.uploads].sort(), [0]);
  assert.deepEqual(env.pastFinal, []);
  assert.deepEqual(env.finalizes, [{ final_sequence: 0, reason: "recorder_lost" }]);
  assert.equal((await load(sessionId))?.needs_operator, true);
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
