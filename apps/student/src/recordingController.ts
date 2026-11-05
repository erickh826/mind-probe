// Browser side of the upload queue. IndexedDB is the source of truth.
// A reload calls `bindSession`, which re-uploads pending slices and a pending
// finalize. It does not call `MediaRecorder.start`.

import { ApiError } from "./apiError.ts";
import {
  deleteChunk,
  deleteUnstartedMeta,
  listChunks,
  openRecordingDb,
  purgeExpiredMeta,
  readMeta,
  storeChunkAndBumpSequence,
  updateMeta,
  writeChunk,
  writeMeta,
  type PersistedChunk,
} from "./recordingDb.ts";
import {
  applyServerStatus,
  chunkRetryDelayMs,
  clearUnauthorizedChunk,
  clearUnauthorizedUploadBlock,
  closeBanner,
  emptyMeta,
  finalizeDue,
  holdForRecorderFlush,
  noteFinalizeAttempt,
  notePermanentFailure,
  permanentBlockMessage,
  permanentUploadBlock,
  persistInterrupted,
  persistRecorderLost,
  persistReplaced,
  persistStop,
  planReload,
  releaseRecorderFlush,
  sequencesAlreadyReceived,
  storageWarning,
  takeAlreadyStarted,
  type CloseBanner,
  type FinalizePayload,
  type RecordingMeta,
  type ServerUploadStatus,
  type UploadBlock,
} from "./uploadQueue.ts";
import type { RecorderStatus } from "./types";

export interface RecordingView {
  status: RecorderStatus;
  stream: MediaStream | null;
  error: string | null;
  uploadedCount: number;
  failedCount: number;
  pendingCount: number;
  banner: CloseBanner;
  storageWarning: string | null;
  canStart: boolean;
  /**
   * The server's word on whether this session already has its take: `free`
   * lets a fresh tab start, `unknown` (not loaded or failed) does not.
   */
  takeCheck: TakeState;
}

export type TakeState = "unknown" | "free" | "claimed";

type Listener = (view: RecordingView) => void;

interface RecorderLike {
  state: string;
  stop: () => void;
}

interface RecordingTransport {
  uploadRecordingChunk: (
    sessionId: string,
    sequence: number,
    chunk: Blob,
    meta: { start_ms: number; end_ms: number; checksum: string },
  ) => Promise<{ status: string }>;
  fetchUploadStatus: (sessionId: string) => Promise<ServerUploadStatus>;
  finalizeRecording: (
    sessionId: string,
    payload: FinalizePayload,
  ) => Promise<{
    recording_state: string;
    finalize_state: ServerUploadStatus["finalize_state"];
    final_sequence: number | null;
    missing_sequences: number[];
  }>;
  claimRecording: (sessionId: string, takeId: string) => Promise<unknown>;
}

const defaultTransport: RecordingTransport = {
  uploadRecordingChunk: async (sessionId, sequence, chunk, meta) => {
    const api = await import("./api.ts");
    return api.uploadRecordingChunk(sessionId, sequence, chunk, meta);
  },
  fetchUploadStatus: async (sessionId) => {
    const api = await import("./api.ts");
    return api.fetchUploadStatus(sessionId);
  },
  finalizeRecording: async (sessionId, payload) => {
    const api = await import("./api.ts");
    return api.finalizeRecording(sessionId, payload);
  },
  claimRecording: async (sessionId, takeId) => {
    const api = await import("./api.ts");
    return api.claimRecording(sessionId, takeId);
  },
};

let databasePromise: Promise<IDBDatabase> | null = null;
let transport: RecordingTransport = defaultTransport;
let active: { sessionId: string; recorder: RecorderLike; stream: MediaStream | null } | null = null;
/** Set only by a pagehide-triggered stop, so onstop records recorder_lost. */
let stopAsLost = false;
let releaseTimer: number | null = null;
let pumpTimer: ReturnType<typeof setTimeout> | null = null;
let pumpGeneration = 0;
/** Chunk upload, including its follow-up status read. Does not gate finalize. */
let chunkTask: Promise<void> | null = null;
/** One finalize POST. A later tick can start one while a chunk request is still pending. */
let finalizeTask: Promise<void> | null = null;
/** A slice could not be stored. Stop must not declare a normal complete-capable take. */
let chunkPersistLost = false;
let boundSession: string | null = null;
let meta: RecordingMeta | null = null;
let pendingCount = 0;
let uploadedCount = 0;
let failedCount = 0;
let errorMessage: string | null = null;
/**
 * Why `errorMessage` is set. Transient chunk failures are remembered per
 * sequence. Confirming the one on screen leaves any other transient failure
 * visible. A held failure (conflict, storage, recorder, lost, finalize) is
 * not cleared by a chunk status read.
 */
let shownError: { kind: "chunk"; sequence: number } | { kind: "held" } | null = null;
const transientChunkErrors = new Map<number, string>();
let warning: string | null = null;
let canStart = true;
let retryable = false;
let status: RecorderStatus = typeof MediaRecorder === "undefined" ? "unavailable" : "idle";
const listeners = new Set<Listener>();
const CHUNK_PERSIST_ATTEMPTS = 3;
const inFlight = new Set<number>();
let chunkUploaded: ((sequence: number, timestampMs: number) => void) | null = null;
let persistChain: Promise<void> = Promise.resolve();
let stopPromise: Promise<void> = Promise.resolve();
/** The session whose onstop is storing its last slices and declaring the take. */
let committingStop: string | null = null;
/** Sessions whose live credential was revoked in this tab. No recorder starts for them again. */
const revokedSessions = new Set<string>();
/** Sessions another device took over from this tab (4409). No recorder starts for them again. */
const replacedSessions = new Set<string>();
/**
 * Sessions whose socket the server has admitted right now. Fed by the socket
 * hook; an open transport is not enough, and neither is holding a live token.
 */
const admittedSessions = new Set<string>();
/**
 * The take this tab has asked the server for and not yet started recording.
 * Memory only: never storage, so a duplicated tab or a reload cannot reuse it.
 */
let pendingTake: { sessionId: string; takeId: string } | null = null;
/** The one start attempt in flight; concurrent starts join it. */
let startTask: Promise<void> | null = null;
let takeCheck: { sessionId: string; state: TakeState } | null = null;
const CLAIM_ATTEMPTS = 3;
const CLAIM_RETRY_MS = 500;
/**
 * Held by the tab whose recorder owns a session's take, for as long as it may
 * write it. Tabs share IndexedDB: without this a duplicated tab reconciling
 * the same meta would declare the live take lost and finalize it.
 */
const RECORDER_LOCK_PREFIX = "mindprobe-recorder:";
const OTHER_TAB_RECHECK_MS = 3_000;
let releaseRecorderLock: (() => void) | null = null;

const REVOKED_MESSAGE =
  "此裝置的連線已被撤銷，錄影已停止。已保存的片段仍可在權限及期限允許時補傳：重新整理頁面，再輸入老師或管理員提供的補傳碼。這段錄影會標記為不完整。";
const REPLACED_MESSAGE =
  "此裝置已被另一個連線取代，錄影已停止。已保存的片段會繼續上傳；這段錄影會標記為不完整。";
const ALREADY_CLAIMED_MESSAGE =
  "此場次已有錄影，換裝置不會接續；如需重新錄影請建立新場次。";
const NOT_ADMITTED_MESSAGE = "尚未連上場次。連線完成後才能開始錄影。";
const TAKE_UNKNOWN_MESSAGE = "無法確認此場次的錄影狀態，請稍後再按 Start REC。";
const SESSION_ENDED_MESSAGE = "場次已結束，無法開始錄影。";
const UNAUTHORIZED_MESSAGE = "此裝置的登入已失效，無法開始錄影。請重新加入場次。";
const CLAIM_FAILED_MESSAGE = "無法向伺服器確認開始錄影，請再按一次 Start REC。";
const START_FAILED_MESSAGE = "錄影器無法啟動，請再按一次 Start REC";
const OTHER_TAB_MESSAGE = "此場次正在另一個分頁錄影。請回到那個分頁，不要在這裡重新開始。";

function webLocks(): LockManager | null {
  return typeof navigator !== "undefined" && navigator.locks ? navigator.locks : null;
}

/** Take the recorder lock for this tab. False when another tab holds it. */
function holdRecorderLock(sessionId: string): Promise<boolean> {
  const locks = webLocks();
  if (locks === null) {
    return Promise.resolve(true);
  }
  dropRecorderLock();
  return new Promise<boolean>((resolve) => {
    void locks
      .request(RECORDER_LOCK_PREFIX + sessionId, { ifAvailable: true }, (lock) => {
        if (lock === null) {
          resolve(false);
          return undefined;
        }
        resolve(true);
        return new Promise<void>((release) => {
          releaseRecorderLock = release;
        });
      })
      .catch(() => resolve(true));
  });
}

function dropRecorderLock(): void {
  releaseRecorderLock?.();
  releaseRecorderLock = null;
}

/** Whether another tab's recorder holds this session's take right now. */
async function recorderHeldElsewhere(sessionId: string): Promise<boolean> {
  const locks = webLocks();
  if (locks === null) {
    return false;
  }
  try {
    const snapshot = await locks.query();
    return (snapshot.held ?? []).some((lock) => lock.name === RECORDER_LOCK_PREFIX + sessionId);
  } catch {
    return false;
  }
}

function newTakeId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function db(): Promise<IDBDatabase> {
  databasePromise ??= openRecordingDb();
  return databasePromise;
}

function view(): RecordingView {
  return {
    status,
    stream: active?.stream ?? null,
    error: errorMessage,
    uploadedCount,
    failedCount,
    pendingCount,
    banner: closeBanner(meta, pendingCount),
    storageWarning: warning,
    canStart:
      canStart &&
      !(boundSession !== null && revokedSessions.has(boundSession)) &&
      !(boundSession !== null && replacedSessions.has(boundSession)) &&
      currentTakeState() !== "claimed",
    takeCheck: currentTakeState(),
  };
}

function currentTakeState(): TakeState {
  if (boundSession === null || takeCheck?.sessionId !== boundSession) {
    return "unknown";
  }
  // A take this tab is still confirming is its own, not someone else's.
  if (pendingTake?.sessionId === boundSession && takeCheck.state === "claimed" && meta === null) {
    return "free";
  }
  return takeCheck.state;
}

/**
 * The socket hook reports admission. A fresh tab with no local take asks the
 * server whether the session already has one, so Start REC and auto-start
 * wait for a definite answer and a failed check is tried again.
 */
export function setSocketAdmitted(sessionId: string, admitted: boolean): void {
  if (admitted) {
    admittedSessions.add(sessionId);
  } else {
    admittedSessions.delete(sessionId);
  }
  if (
    admitted &&
    boundSession === sessionId &&
    meta === null &&
    currentTakeState() === "unknown"
  ) {
    void checkTake(sessionId).then(() => {
      showClaimedElsewhere(sessionId);
      emit();
    });
  }
  emit();
}

/** A fresh tab learned the take exists: say so, rather than just hiding Start REC. */
function showClaimedElsewhere(sessionId: string): void {
  if (
    boundSession === sessionId &&
    meta === null &&
    pendingTake?.sessionId !== sessionId &&
    takeCheck?.sessionId === sessionId &&
    takeCheck.state === "claimed"
  ) {
    lockAlreadyClaimed(sessionId);
  }
}

function emit(): void {
  const snapshot = view();
  for (const listener of listeners) {
    listener(snapshot);
  }
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  listener(view());
  return () => listeners.delete(listener);
}

export function errorIsRetryable(): boolean {
  return retryable;
}

export function setChunkUploadedListener(
  listener: ((sequence: number, timestampMs: number) => void) | null,
): void {
  chunkUploaded = listener;
}

async function sha256Hex(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function pickMimeType(): string | undefined {
  const candidates = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
  ];
  return candidates.find((candidate) => MediaRecorder.isTypeSupported(candidate));
}

function describeMediaError(cause: unknown): { message: string; retryable: boolean } {
  const name = cause instanceof DOMException ? cause.name : "";
  switch (name) {
    case "NotFoundError":
    case "OverconstrainedError":
      return {
        message: "No camera/microphone found. Connect one and recording will start automatically.",
        retryable: true,
      };
    case "NotReadableError":
    case "AbortError":
      return {
        message: "Camera is busy or unavailable (used by another app/tab?). Close it; retrying automatically.",
        retryable: true,
      };
    case "NotAllowedError":
    case "SecurityError":
      return {
        message: "Camera/microphone permission denied. Allow access in the browser address bar, then press Start.",
        retryable: false,
      };
    default:
      return { message: `Unable to start recording: ${String(cause)}`, retryable: false };
  }
}

function cancelRelease(): void {
  if (releaseTimer !== null) {
    window.clearTimeout(releaseTimer);
    releaseTimer = null;
  }
}

export function scheduleRelease(): void {
  cancelRelease();
  const sessionId = boundSession;
  releaseTimer = window.setTimeout(() => {
    releaseTimer = null;
    if (sessionId !== null && active?.sessionId === sessionId) {
      stopRecording();
    }
  }, 50);
}

function ensurePump(sessionId: string): void {
  if (pumpTimer !== null) {
    return;
  }
  // The next tick is scheduled immediately, not when the previous pump's
  // network calls finish. A chunk upload or status read must not postpone
  // the finalize deadline.
  const generation = pumpGeneration;
  const tick = () => {
    if (generation !== pumpGeneration || boundSession !== sessionId) {
      return;
    }
    pumpTimer = setTimeout(() => {
      if (generation !== pumpGeneration || boundSession !== sessionId) {
        return;
      }
      void pump(sessionId).catch(notePumpFailure);
      tick();
    }, 400);
  };
  void pump(sessionId).catch(notePumpFailure);
  tick();
}

function rememberChunkFailure(sequence: number, message: string): void {
  transientChunkErrors.set(sequence, message);
  errorMessage = message;
  shownError = { kind: "chunk", sequence };
}

function rememberHeldFailure(message: string): void {
  errorMessage = message;
  shownError = { kind: "held" };
}

function forgetError(): void {
  errorMessage = null;
  shownError = null;
  transientChunkErrors.clear();
}

/**
 * After a status read, drop transient messages whose slice is confirmed and
 * no longer queued. If another transient failure is still queued, show that
 * one. A missing listing does not clear, even when the local failed count is 0.
 */
function syncTransientChunkError(received: readonly number[], localChunks: readonly PersistedChunk[]): void {
  if (shownError == null || shownError.kind !== "chunk") {
    return;
  }
  const queued = new Set(localChunks.map((chunk) => chunk.sequence));
  for (const sequence of [...transientChunkErrors.keys()]) {
    if (!queued.has(sequence) && received.includes(sequence)) {
      transientChunkErrors.delete(sequence);
    }
  }
  if (transientChunkErrors.has(shownError.sequence)) {
    return;
  }
  const waiting = localChunks.filter((chunk) => chunk.attempts > 0 && !chunk.terminal);
  if (waiting.length > 0) {
    const next = waiting.find((chunk) => transientChunkErrors.has(chunk.sequence)) ?? waiting[0];
    const stored = transientChunkErrors.get(next.sequence);
    const message = stored ?? `尚有 ${waiting.length} 段等待重試`;
    if (stored == null) {
      transientChunkErrors.set(next.sequence, message);
    }
    errorMessage = message;
    shownError = { kind: "chunk", sequence: next.sequence };
    return;
  }
  errorMessage = null;
  shownError = null;
}

function notePumpFailure(cause: unknown): void {
  rememberHeldFailure(`上傳佇列中斷：${cause instanceof Error ? cause.message : String(cause)}`);
}

async function refreshPending(sessionId: string): Promise<PersistedChunk[]> {
  const database = await db();
  const chunks = await listChunks(database, sessionId);
  pendingCount = chunks.length;
  failedCount = chunks.filter((chunk) => chunk.attempts > 0 && !chunk.terminal).length;
  const bytes = chunks.reduce((sum, chunk) => sum + chunk.blob.size, 0);
  warning = storageWarning(bytes);
  return chunks;
}

async function reconcile(sessionId: string, now: number): Promise<void> {
  const database = await db();
  let current = await readMeta(database, sessionId);
  if (current && (await purgeExpiredMeta(database, current, now))) {
    meta = null;
    canStart = true;
    return;
  }
  if (current === null) {
    meta = null;
    canStart = true;
    if (admittedSessions.has(sessionId) && currentTakeState() === "unknown") {
      await checkTake(sessionId);
      showClaimedElsewhere(sessionId);
      emit();
    }
    return;
  }
  const recorderHere = active?.sessionId === sessionId && active.recorder.state !== "inactive";
  // This tab's recorder is stopping: its onstop stores the last slices and
  // owns the declaration.
  const stopPendingHere =
    !recorderHere && (active?.sessionId === sessionId || committingStop === sessionId);
  // A duplicated tab shares this meta. While the tab that records it holds
  // the lock, its take is alive, not lost, and a held lost declaration still
  // waits for its last slices; look again once the lock is free.
  const recordingElsewhere =
    !recorderHere &&
    !stopPendingHere &&
    (current.recorder_state === "recording" || current.recorder_flush_pending === true) &&
    (await recorderHeldElsewhere(sessionId));
  if (recordingElsewhere) {
    meta = current;
    canStart = false;
    status = "idle";
    rememberHeldFailure(OTHER_TAB_MESSAGE);
    setTimeout(() => {
      if (boundSession === sessionId) {
        void reconcile(sessionId, Date.now()).then(() => emit());
      }
    }, OTHER_TAB_RECHECK_MS);
    return;
  }
  const recorderStillHere = recorderHere || stopPendingHere;
  try {
    const serverStatus = await transport.fetchUploadStatus(sessionId);
    const chunks = await listChunks(database, sessionId);
    for (const sequence of sequencesAlreadyReceived(
      chunks.map((chunk) => chunk.sequence),
      serverStatus.received_sequences,
    )) {
      await deleteChunk(database, sessionId, sequence);
    }
    const updated = await updateMeta(database, sessionId, (latest) =>
      mergeReload(latest, serverStatus, now, recorderStillHere),
    );
    current = updated ?? current;
  } catch (cause) {
    rememberHeldFailure(`無法讀取上傳狀態：${cause instanceof Error ? cause.message : String(cause)}`);
    const updated = await updateMeta(database, sessionId, (latest) =>
      mergeReload(latest, null, now, recorderStillHere),
    );
    current = updated ?? current;
  }
  meta = current;
  canStart = false;
  if (recorderHere) {
    status = "recording";
  } else if (stopPendingHere) {
    status = "stopping";
  } else if (planReload(current).kind !== "idle") {
    status = "stopped";
  }
}

function mergeReload(
  latest: RecordingMeta,
  serverStatus: ServerUploadStatus | null,
  now: number,
  recorderStillHere: boolean,
): RecordingMeta {
  let next = serverStatus ? applyServerStatus(latest, serverStatus, now) : latest;
  // A remount while MediaRecorder is still running is not a lost take.
  if (planReload(next).kind === "recorder_lost" && !recorderStillHere) {
    next = persistRecorderLost(next, now);
  }
  // No recorder is left to store the slices a held declaration waited for
  // (the page that ran it is gone): declare what was stored.
  if (!recorderStillHere) {
    next = releaseRecorderFlush(next, now);
  }
  return next;
}

/** Drop only unauthorized blocks after a successful upload-login. */
export async function resumeUnauthorizedUploads(
  sessionId: string,
  now = Date.now(),
): Promise<RecordingMeta | null> {
  const database = await db();
  const chunks = await listChunks(database, sessionId);
  const opened = chunks.map((chunk) => clearUnauthorizedChunk(chunk, now));
  for (let index = 0; index < chunks.length; index += 1) {
    if (opened[index] !== chunks[index]) {
      await writeChunk(database, opened[index]);
    }
  }
  const remainingTerminal = opened.flatMap((chunk) =>
    chunk.terminal && chunk.terminal_reason && chunk.terminal_reason !== "unauthorized"
      ? [chunk.terminal_reason]
      : [],
  );
  const updated = await updateMeta(database, sessionId, (latest) =>
    clearUnauthorizedUploadBlock(latest, now, remainingTerminal),
  );
  if (updated && boundSession === sessionId) {
    meta = updated;
    forgetError();
    await refreshPending(sessionId);
    emit();
  }
  return updated;
}

export async function bindSession(sessionId: string): Promise<void> {
  cancelRelease();
  if (boundSession !== sessionId) {
    if (pumpTimer !== null) {
      clearTimeout(pumpTimer);
      pumpTimer = null;
    }
    uploadedCount = 0;
    failedCount = 0;
    pendingCount = 0;
    forgetError();
    chunkTask = null;
    finalizeTask = null;
  }
  boundSession = sessionId;
  // Status reconciliation can sit on a revoked session endpoint or a slow
  // network. Uploads and finalize use the IndexedDB row and must not wait for it.
  void reconcile(sessionId, Date.now());
  try {
    await refreshPending(sessionId);
    ensurePump(sessionId);
  } catch (cause) {
    rememberHeldFailure(`錄影佇列無法開啟：${cause instanceof Error ? cause.message : String(cause)}`);
    status = "error";
  }
  emit();
}

export async function persistBlob(sessionId: string, blob: Blob): Promise<void> {
  if (blob.size === 0) {
    return;
  }
  const database = await db();
  const current = (await readMeta(database, sessionId)) ?? meta;
  const startedAt = current?.recorder_started_at ?? Date.now();
  const checksum = await sha256Hex(blob);
  const stored = await storeChunkAndBumpSequence(database, sessionId, blob, {
    end_ms: Math.max(0, Date.now() - startedAt),
    checksum,
    now: Date.now(),
  });
  meta = stored.meta;
  await refreshPending(sessionId);
  emit();
}

async function markDurableChunkLoss(sessionId: string): Promise<void> {
  const database = await db();
  const running = active?.sessionId === sessionId && active.recorder.state !== "inactive";
  const updated = await updateMeta(database, sessionId, (latest) => {
    const lost = { ...persistRecorderLost(latest, Date.now()), needs_operator: true };
    return running ? holdForRecorderFlush(lost) : lost;
  });
  if (updated) {
    meta = updated;
  }
}

/** Keep the blob if IndexedDB accepts a retry. Otherwise the take is lost for good. */
async function retainChunk(sessionId: string, blob: Blob): Promise<void> {
  let last: unknown;
  for (let attempt = 1; attempt <= CHUNK_PERSIST_ATTEMPTS; attempt += 1) {
    try {
      await persistBlob(sessionId, blob);
      return;
    } catch (cause) {
      last = cause;
    }
  }
  chunkPersistLost = true;
  await markDurableChunkLoss(sessionId);
  throw last instanceof Error ? last : new Error(String(last));
}

function attachRecorder(sessionId: string, recorder: MediaRecorder, stream: MediaStream | null): void {
  active = { sessionId, recorder, stream };
  persistChain = Promise.resolve();
  recorder.ondataavailable = (event) => {
    const blob = event.data;
    if (blob.size === 0) {
      return;
    }
    persistChain = persistChain
      .then(() => retainChunk(sessionId, blob))
      .catch((cause: unknown) => {
        // The chain stays usable for a later slice, but the take is already
        // marked lost. A normal stop must not advertise the missing slice.
        chunkPersistLost = true;
        rememberHeldFailure(`無法保存錄影片段：${String(cause)}`);
        emit();
      });
  };
  recorder.onerror = () => {
    rememberHeldFailure("MediaRecorder error.");
    status = "error";
    emit();
  };
  recorder.onstop = () => {
    stopPromise = handleRecorderStop(sessionId, stream);
  };
}

/**
 * Start this tab's one take. Concurrent calls -- a double click, auto-start,
 * the 5 s camera retry -- share one attempt, so two recorders never start.
 */
export function startRecording(sessionId: string): Promise<void> {
  startTask ??= startRecordingOnce(sessionId).finally(() => {
    startTask = null;
  });
  return startTask;
}

function stopTracks(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

/** Why this tab may not start right now, or null. Checked before and after each wait. */
function startBlocked(sessionId: string): string | null {
  if (revokedSessions.has(sessionId)) {
    return REVOKED_MESSAGE;
  }
  if (replacedSessions.has(sessionId)) {
    return REPLACED_MESSAGE;
  }
  if (!admittedSessions.has(sessionId)) {
    return NOT_ADMITTED_MESSAGE;
  }
  return null;
}

function holdStartRefusal(message: string): void {
  rememberHeldFailure(message);
  retryable = false;
  status = "idle";
}

function lockAlreadyClaimed(sessionId: string): void {
  takeCheck = { sessionId, state: "claimed" };
  canStart = false;
  holdStartRefusal(ALREADY_CLAIMED_MESSAGE);
}

async function startRecordingOnce(sessionId: string): Promise<void> {
  cancelRelease();
  if (revokedSessions.has(sessionId) || replacedSessions.has(sessionId)) {
    emit();
    return;
  }
  if (active && active.sessionId === sessionId && active.recorder.state !== "inactive") {
    status = "recording";
    emit();
    return;
  }
  await bindSession(sessionId);
  const database = await db();
  const existing = await readMeta(database, sessionId);
  if (existing !== null) {
    // One take per session. The original recorder is gone; do not start another.
    canStart = false;
    meta = existing;
    emit();
    return;
  }
  const blocked = startBlocked(sessionId);
  if (blocked !== null) {
    holdStartRefusal(blocked);
    emit();
    return;
  }
  // D3: a take this tab is still confirming skips only the "already claimed"
  // check for its own id. Everything else is asked of the server again.
  const pendingId = pendingTake?.sessionId === sessionId ? pendingTake.takeId : null;
  if (pendingId === null) {
    const state = await checkTake(sessionId);
    if (state === "claimed") {
      lockAlreadyClaimed(sessionId);
      emit();
      return;
    }
    if (state !== "free") {
      holdStartRefusal(TAKE_UNKNOWN_MESSAGE);
      emit();
      return;
    }
  }
  if (typeof MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia) {
    status = "unavailable";
    rememberHeldFailure("MediaRecorder is not available in this browser.");
    emit();
    return;
  }
  status = "requesting";
  forgetError();
  retryable = false;
  emit();
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  } catch (cause) {
    if (revokedSessions.has(sessionId)) {
      // The prompt failed after the revocation. Keep the revocation in view, no retry.
      status = "idle";
    } else {
      const described = describeMediaError(cause);
      rememberHeldFailure(described.message);
      retryable = described.retryable;
      status = "error";
    }
    emit();
    return;
  }
  const blockedAfterPrompt = startBlocked(sessionId);
  if (blockedAfterPrompt !== null) {
    // Revoked, replaced, or disconnected while the permission prompt was open.
    stopTracks(stream);
    holdStartRefusal(blockedAfterPrompt);
    emit();
    return;
  }
  const again = await readMeta(database, sessionId);
  if (again !== null) {
    stopTracks(stream);
    canStart = false;
    meta = again;
    status = "idle";
    emit();
    return;
  }

  // Claim after the camera is granted, so a refused camera never uses up the take.
  const takeId = pendingId ?? newTakeId();
  pendingTake = { sessionId, takeId };
  const claim = await claimWithRetry(sessionId, takeId);
  if (claim !== "claimed" && claim !== "legacy") {
    stopTracks(stream);
    await refuseClaim(sessionId, claim);
    emit();
    return;
  }
  const blockedAfterClaim = startBlocked(sessionId);
  if (blockedAfterClaim !== null) {
    // The claim stands (it is never released); this tab just may not record now.
    stopTracks(stream);
    holdStartRefusal(blockedAfterClaim);
    emit();
    return;
  }
  takeCheck = { sessionId, state: "claimed" };
  if (!(await holdRecorderLock(sessionId))) {
    // Another tab of this browser is recording this take already.
    stopTracks(stream);
    canStart = false;
    holdStartRefusal(OTHER_TAB_MESSAGE);
    emit();
    return;
  }

  let recorder: MediaRecorder;
  let created: RecordingMeta;
  try {
    const mimeType = pickMimeType();
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    created = emptyMeta(sessionId, Date.now());
    await writeMeta(database, created);
  } catch (cause) {
    // Nothing was captured. The claim is kept and this tab may retry it.
    dropRecorderLock();
    stopTracks(stream);
    rememberHeldFailure(`${START_FAILED_MESSAGE}（${String(cause)}）`);
    status = "error";
    emit();
    return;
  }
  meta = created;
  canStart = false;
  attachRecorder(sessionId, recorder, stream);
  try {
    recorder.start(2_000);
  } catch (cause) {
    await abandonUnstartedRecorder(sessionId, recorder, stream, created);
    rememberHeldFailure(`${START_FAILED_MESSAGE}（${String(cause)}）`);
    status = "error";
    emit();
    return;
  }
  // start() returned: from here on any failure is a lost take, never a restart.
  pendingTake = null;
  status = "recording";
  ensurePump(sessionId);
  if (revokedSessions.has(sessionId)) {
    // Revoked during the meta write above: stop the take that just began.
    await interruptRecording(sessionId);
    return;
  }
  if (replacedSessions.has(sessionId)) {
    await stopReplacedRecording(sessionId);
    return;
  }
  emit();
}

/**
 * `MediaRecorder.start()` threw, so it never captured. Fence the recorder off,
 * let anything already queued settle, and remove only the meta this attempt
 * wrote. The same tab may then retry with the same pending take id.
 */
async function abandonUnstartedRecorder(
  sessionId: string,
  recorder: MediaRecorder,
  stream: MediaStream,
  created: RecordingMeta,
): Promise<void> {
  recorder.ondataavailable = null;
  recorder.onstop = null;
  recorder.onerror = null;
  stopTracks(stream);
  if (active?.recorder === recorder) {
    active = null;
  }
  await persistChain.catch(() => undefined);
  dropRecorderLock();
  const database = await db();
  const removed = await deleteUnstartedMeta(
    database,
    sessionId,
    created.recorder_started_at ?? -1,
  );
  if (removed) {
    meta = null;
    canStart = true;
  } else {
    // A slice was stored after all, so capture did happen: keep it, declare
    // the take lost, and never restart it.
    meta = await updateMeta(database, sessionId, (latest) => persistRecorderLost(latest, Date.now()));
    canStart = false;
  }
}

/** The server's word on this session's take: claimed, free, or unknown (failed). */
async function checkTake(sessionId: string): Promise<TakeState> {
  try {
    const serverStatus = await transport.fetchUploadStatus(sessionId);
    const state: TakeState = takeAlreadyStarted(serverStatus) ? "claimed" : "free";
    takeCheck = { sessionId, state };
    return state;
  } catch {
    takeCheck = { sessionId, state: "unknown" };
    return "unknown";
  }
}

type ClaimOutcome =
  | "claimed"
  | "legacy"
  | "already_claimed"
  | "ended"
  | "not_admitted"
  | "revoked"
  | "unauthorized"
  | "failed";

async function claimWithRetry(sessionId: string, takeId: string): Promise<ClaimOutcome> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      await transport.claimRecording(sessionId, takeId);
      return "claimed";
    } catch (cause) {
      const outcome = classifyClaimError(cause);
      if (outcome !== "failed" || attempt >= CLAIM_ATTEMPTS) {
        return outcome;
      }
      await new Promise((resolve) => setTimeout(resolve, CLAIM_RETRY_MS * attempt));
    }
  }
}

function classifyClaimError(cause: unknown): ClaimOutcome {
  if (!(cause instanceof ApiError)) {
    return "failed";
  }
  if (cause.status === 409) {
    switch (cause.code) {
      case "recording_already_claimed":
        return "already_claimed";
      case "session_ended":
        return "ended";
      case "role_not_admitted":
        return "not_admitted";
      default:
        return "already_claimed";
    }
  }
  if (cause.status === 401) {
    return isRevokedCredential(cause) ? "revoked" : "unauthorized";
  }
  // A server from before claims existed has no such route. Record as before;
  // its first chunk still makes the take visible to other devices.
  if (cause.status === 405 || (cause.status === 404 && cause.code === "Not Found")) {
    return "legacy";
  }
  return "failed";
}

async function refuseClaim(sessionId: string, outcome: ClaimOutcome): Promise<void> {
  switch (outcome) {
    case "already_claimed":
      pendingTake = null;
      lockAlreadyClaimed(sessionId);
      return;
    case "ended":
      pendingTake = null;
      canStart = false;
      holdStartRefusal(SESSION_ENDED_MESSAGE);
      return;
    case "not_admitted":
      // Keep the pending id: the next admission may confirm it.
      holdStartRefusal(NOT_ADMITTED_MESSAGE);
      return;
    case "revoked":
      pendingTake = null;
      status = "idle";
      await interruptRecording(sessionId);
      return;
    case "unauthorized":
      pendingTake = null;
      canStart = false;
      holdStartRefusal(UNAUTHORIZED_MESSAGE);
      return;
    default:
      // The claim may have landed with its answer lost: keep the id for the retry.
      holdStartRefusal(CLAIM_FAILED_MESSAGE);
  }
}

/** A 401 that no retry with this live token can get past (ADR-0004 §3). */
export function isRevokedCredential(cause: unknown): boolean {
  return cause instanceof ApiError && cause.status === 401 && cause.code === "revoked_session_token";
}

/**
 * The socket close reason for a revoked token, at upgrade or mid-session.
 * `session_ended` is not one: grace uploads continue after a normal end.
 */
export function isRevokedCloseReason(reason: string | null): boolean {
  return reason === "token_revoked";
}

function blockMessage(sessionId: string, block: UploadBlock): string {
  return block === "unauthorized" && revokedSessions.has(sessionId)
    ? REVOKED_MESSAGE
    : permanentBlockMessage(block);
}

/**
 * Stop this tab's take because its live credential was revoked, for example
 * by an OP join-code reissue with revoke_live. Safe to call more than once.
 *
 * The take is written as lost before the recorder stops, so a hung onstop or
 * a closed tab still reports recorder_lost. onstop extends the declaration
 * over the last slice. Stored slices and their auth blocks stay as they are,
 * so an upload code can still send them. A recorder already stopping
 * normally is left to finish its normal stop.
 */
export async function interruptRecording(sessionId: string): Promise<void> {
  revokedSessions.add(sessionId);
  retryable = false;
  const current = active?.sessionId === sessionId ? active : null;
  if (current && current.recorder.state !== "inactive") {
    const database = await db();
    // Held: the recorder still flushes slices this declaration does not cover.
    const updated = await updateMeta(database, sessionId, (latest) =>
      holdForRecorderFlush(persistInterrupted(latest, Date.now())),
    );
    if (updated) {
      meta = updated;
    }
    status = "stopping";
    try {
      current.recorder.stop();
    } catch {
      // Already stopping. onstop still sees the lost row.
    }
    current.stream?.getTracks().forEach((track) => track.stop());
  }
  if (boundSession === sessionId) {
    rememberHeldFailure(REVOKED_MESSAGE);
  }
  emit();
}

/**
 * Stop this tab's take because another device took the student role over
 * (close 4409 role_connection_replaced). Safe to call more than once.
 *
 * Like a revocation, the take is written as lost before the recorder stops,
 * and onstop extends it over the last slice. Unlike one, the live token still
 * works: nothing is marked unauthorized, and the stored slices keep uploading
 * and finalize with recorder_lost. The take does not continue on the new device.
 */
export async function stopReplacedRecording(sessionId: string): Promise<void> {
  replacedSessions.add(sessionId);
  retryable = false;
  if (pendingTake?.sessionId === sessionId) {
    pendingTake = null;
  }
  const current = active?.sessionId === sessionId ? active : null;
  if (current && current.recorder.state !== "inactive") {
    const database = await db();
    // Held: the live token could finalize this before the recorder's last
    // slices are stored, and the server would then refuse them.
    const updated = await updateMeta(database, sessionId, (latest) =>
      holdForRecorderFlush(persistReplaced(latest, Date.now())),
    );
    if (updated) {
      meta = updated;
    }
    status = "stopping";
    try {
      current.recorder.stop();
    } catch {
      // Already stopping. onstop still sees the lost row.
    }
    current.stream?.getTracks().forEach((track) => track.stop());
  }
  if (boundSession === sessionId) {
    rememberHeldFailure(REPLACED_MESSAGE);
  }
  emit();
}

export function stopRecording(): void {
  const recorder = active?.recorder;
  if (recorder && recorder.state !== "inactive") {
    status = "stopping";
    emit();
    recorder.stop();
    return;
  }
  active?.stream?.getTracks().forEach((track) => track.stop());
  active = null;
  emit();
}

export async function retryFailed(): Promise<void> {
  if (boundSession === null) {
    return;
  }
  const database = await db();
  const chunks = await listChunks(database, boundSession);
  const now = Date.now();
  for (const chunk of chunks) {
    if (chunk.attempts > 0 && !chunk.terminal) {
      await writeChunk(database, { ...chunk, next_attempt_at: now });
    }
  }
  await pump(boundSession);
}

function kickChunk(sessionId: string): Promise<void> {
  if (chunkTask) {
    return chunkTask;
  }
  chunkTask = uploadNextChunk(sessionId).finally(() => {
    chunkTask = null;
  });
  return chunkTask;
}

function kickFinalize(sessionId: string): Promise<void> {
  if (finalizeTask) {
    return finalizeTask;
  }
  finalizeTask = sendFinalizeIfDue(sessionId).finally(() => {
    finalizeTask = null;
  });
  return finalizeTask;
}

async function pump(sessionId: string): Promise<void> {
  if (boundSession !== sessionId) {
    return;
  }
  const database = await db();
  const current = await readMeta(database, sessionId);
  if (current && (await purgeExpiredMeta(database, current, Date.now()))) {
    meta = null;
    pendingCount = 0;
    emit();
    return;
  }
  // Start both before awaiting either. A pending upload or status read must
  // not move the finalize deadline or skip the POST.
  const uploading = kickChunk(sessionId);
  const finalizing = kickFinalize(sessionId);
  await Promise.all([uploading, finalizing]);
  emit();
}

async function uploadNextChunk(sessionId: string): Promise<void> {
  const database = await db();
  const chunks = await refreshPending(sessionId);
  const now = Date.now();
  const next = chunks.find(
    (chunk) => !chunk.terminal && !inFlight.has(chunk.sequence) && now >= chunk.next_attempt_at,
  );
  if (!next) {
    return;
  }
  inFlight.add(next.sequence);
  try {
    await transport.uploadRecordingChunk(sessionId, next.sequence, next.blob, {
      start_ms: next.start_ms,
      end_ms: next.end_ms,
      checksum: next.checksum,
    });
    await deleteChunk(database, sessionId, next.sequence);
    uploadedCount += 1;
    chunkUploaded?.(next.sequence, next.end_ms);
    let received: number[] | null = null;
    try {
      const serverStatus = await transport.fetchUploadStatus(sessionId);
      received = serverStatus.received_sequences;
      const updated = await updateMeta(database, sessionId, (latest) =>
        applyServerStatus(latest, serverStatus, Date.now()),
      );
      if (updated) {
        meta = updated;
      }
    } catch {
      // The blob is gone locally. The next finalize or status read will catch up.
    }
    if (received) {
      syncTransientChunkError(received, await listChunks(database, sessionId));
    }
  } catch (cause) {
    const block = cause instanceof ApiError ? permanentUploadBlock(cause.status, cause.code) : null;
    if (block) {
      await writeChunk(database, {
        ...next,
        attempts: next.attempts + 1,
        terminal: true,
        terminal_reason: block,
        next_attempt_at: Number.MAX_SAFE_INTEGER,
      });
      const updated = await updateMeta(database, sessionId, (latest) =>
        notePermanentFailure(latest, block),
      );
      if (updated) {
        meta = updated;
      }
      rememberHeldFailure(blockMessage(sessionId, block));
      if (isRevokedCredential(cause) && active?.sessionId === sessionId) {
        // Awaited, so the lost declaration is on disk when this upload returns.
        await interruptRecording(sessionId);
      }
    } else {
      const attempts = next.attempts + 1;
      await writeChunk(database, {
        ...next,
        attempts,
        next_attempt_at: Date.now() + chunkRetryDelayMs(attempts),
      });
      failedCount = (await listChunks(database, sessionId)).filter(
        (chunk) => chunk.attempts > 0 && !chunk.terminal,
      ).length;
      rememberChunkFailure(
        next.sequence,
        cause instanceof ApiError
          ? `片段 ${next.sequence} 上傳失敗：${cause.message}`
          : `片段 ${next.sequence} 上傳失敗：${String(cause)}`,
      );
    }
  } finally {
    inFlight.delete(next.sequence);
    await refreshPending(sessionId);
  }
}

async function sendFinalizeIfDue(sessionId: string): Promise<void> {
  const database = await db();
  const current = await readMeta(database, sessionId);
  const pending = await refreshPending(sessionId);
  meta = current;
  if (!current || !current.finalize_payload || !finalizeDue(current, pending.length, Date.now())) {
    return;
  }
  const payload = current.finalize_payload;
  const persistedAt = current.finalize_persisted_at;
  try {
    const result = await transport.finalizeRecording(sessionId, payload);
    const stillOpen = result.finalize_state === "pending" || result.finalize_state === "none";
    const updated = await updateMeta(database, sessionId, (latest) => {
      // A stop that landed during the POST owns the payload. Do not confirm
      // a declaration the server has not seen, and do not move the deadline.
      if (!sameFinalizePayload(latest.finalize_payload, payload)) {
        return latest;
      }
      if (persistedAt != null && latest.finalize_persisted_at !== persistedAt) {
        return latest;
      }
      return applyServerStatus(
        noteFinalizeAttempt(latest, Date.now(), stillOpen),
        {
          finalize_state: result.finalize_state,
          recording_state: result.recording_state,
          received_sequences: [],
        },
        Date.now(),
      );
    });
    if (updated) {
      meta = updated;
    }
    forgetError();
    if (replacedSessions.has(sessionId)) {
      // Still true after the finalize: this tab stays replaced and never records again.
      rememberHeldFailure(REPLACED_MESSAGE);
    }
  } catch (cause) {
    const block = cause instanceof ApiError ? permanentUploadBlock(cause.status, cause.code) : null;
    // Chunk retries must not move finalize_persisted_at. Only the attempt clock moves.
    let updated: RecordingMeta | null = null;
    try {
      updated = await updateMeta(database, sessionId, (latest) =>
        block ? notePermanentFailure(latest, block) : noteFinalizeAttempt(latest, Date.now(), true),
      );
    } catch {
      // The page is closing or the test reset the database under an in-flight POST.
      updated = null;
    }
    if (updated) {
      meta = updated;
    }
    rememberHeldFailure(
      block
        ? blockMessage(sessionId, block)
        : `結束標記送出失敗：${cause instanceof Error ? cause.message : String(cause)}`,
    );
  }
}

function sameFinalizePayload(left: FinalizePayload | null, right: FinalizePayload): boolean {
  if (left == null) {
    return false;
  }
  return (
    left.final_sequence === right.final_sequence &&
    left.total_chunks === right.total_chunks &&
    left.total_duration_ms === right.total_duration_ms &&
    (left.reason ?? null) === (right.reason ?? null)
  );
}

export async function commitRecorderStop(
  sessionId: string,
  now = Date.now(),
): Promise<RecordingMeta | null> {
  const lost = stopAsLost || chunkPersistLost;
  stopAsLost = false;
  const database = await db();
  const updated = await updateMeta(database, sessionId, (latest) =>
    // Every slice is stored now; a held declaration may go out.
    releaseRecorderFlush(declareStop(latest, lost, sessionId, now), now),
  );
  if (updated) {
    meta = updated;
  }
  return updated;
}

function declareStop(
  latest: RecordingMeta,
  lost: boolean,
  sessionId: string,
  now: number,
): RecordingMeta {
  if (latest.recorder_state === "lost") {
    // A revocation or a takeover declared the take lost before the recorder
    // flushed its last slice: extend the declaration over that slice.
    const kept = revokedSessions.has(sessionId)
      ? persistInterrupted(latest, now)
      : replacedSessions.has(sessionId)
        ? persistReplaced(latest, now)
        : latest;
    return chunkPersistLost ? { ...kept, needs_operator: true } : kept;
  }
  if (lost) {
    const marked = persistRecorderLost(latest, now);
    return chunkPersistLost ? { ...marked, needs_operator: true } : marked;
  }
  if (latest.finalize_pending && latest.finalize_persisted_at != null) {
    return latest;
  }
  return persistStop(latest, now);
}

export function notePageHide(): void {
  if (active && active.recorder.state !== "inactive") {
    stopAsLost = true;
    try {
      active.recorder.stop();
    } catch {
      // stop() was already in progress. onstop still sees stopAsLost.
    }
  }
}

export function handleRecorderStop(sessionId: string, stream: MediaStream | null): Promise<void> {
  committingStop = sessionId;
  status = "stopped";
  stream?.getTracks().forEach((track) => track.stop());
  if (active?.sessionId === sessionId) {
    active = null;
  }
  return persistChain
    .then(() => commitRecorderStop(sessionId))
    .then(() => undefined)
    .catch((cause: unknown) => {
      rememberHeldFailure(`無法保存結束標記：${String(cause)}`);
    })
    .finally(() => {
      // The take is declared; another tab may now judge it.
      if (committingStop === sessionId) {
        committingStop = null;
      }
      dropRecorderLock();
      emit();
    });
}

export function setRecordingTransport(next: Partial<RecordingTransport> | null): void {
  transport = next ? { ...defaultTransport, ...next } : defaultTransport;
}

export function installActiveRecorderForTests(sessionId: string, recorder: RecorderLike): void {
  active = { sessionId, recorder, stream: null };
  boundSession = sessionId;
}

/** Wire the production ondataavailable / onstop handlers around a fake recorder. */
export function attachRecorderForTests(sessionId: string, recorder: MediaRecorder): void {
  attachRecorder(sessionId, recorder, null);
}

export function recorderStopPromiseForTests(): Promise<void> {
  return stopPromise;
}

export async function pumpSession(sessionId: string): Promise<void> {
  boundSession = sessionId;
  await pump(sessionId);
}

export async function resetRecordingControllerForTests(
  options?: { keepDatabase?: boolean },
): Promise<void> {
  pumpGeneration += 1;
  boundSession = null;
  if (pumpTimer !== null) {
    clearTimeout(pumpTimer);
  }
  pumpTimer = null;
  const inFlightWork = [chunkTask, finalizeTask];
  chunkTask = null;
  finalizeTask = null;
  chunkPersistLost = false;
  // Let an in-flight finalize write finish before the database is closed.
  await Promise.allSettled(inFlightWork);
  const pending = databasePromise;
  databasePromise = null;
  if (pending) {
    try {
      (await pending).close();
    } catch {
      // The database may already be closed.
    }
  }
  if (!options?.keepDatabase && typeof indexedDB !== "undefined") {
    await new Promise<void>((resolve) => {
      const request = indexedDB.deleteDatabase("mindprobe-recording");
      request.onsuccess = () => resolve();
      request.onerror = () => resolve();
      request.onblocked = () => resolve();
    });
  }
  transport = defaultTransport;
  active = null;
  stopAsLost = false;
  meta = null;
  pendingCount = 0;
  uploadedCount = 0;
  failedCount = 0;
  forgetError();
  warning = null;
  canStart = true;
  retryable = false;
  status = typeof MediaRecorder === "undefined" ? "unavailable" : "idle";
  inFlight.clear();
  persistChain = Promise.resolve();
  stopPromise = Promise.resolve();
  committingStop = null;
  revokedSessions.clear();
  replacedSessions.clear();
  admittedSessions.clear();
  pendingTake = null;
  startTask = null;
  takeCheck = null;
  dropRecorderLock();
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    notePageHide();
  });
}

export function recordingView(): RecordingView {
  return view();
}
