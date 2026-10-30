// Browser side of the upload queue. IndexedDB is the source of truth.
// A reload calls `bindSession`, which re-uploads pending slices and a pending
// finalize. It does not call `MediaRecorder.start`.

import { ApiError } from "./apiError.ts";
import {
  deleteChunk,
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
  noteFinalizeAttempt,
  notePermanentFailure,
  permanentBlockMessage,
  permanentUploadBlock,
  persistInterrupted,
  persistRecorderLost,
  persistStop,
  planReload,
  sequencesAlreadyReceived,
  storageWarning,
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
}

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
/** Sessions whose live credential was revoked in this tab. No recorder starts for them again. */
const revokedSessions = new Set<string>();

const REVOKED_MESSAGE =
  "此裝置的連線已被撤銷，錄影已停止。已保存的片段仍可在權限及期限允許時用補傳碼上傳，但這段錄影會標記為不完整。";

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
    canStart: canStart && !(boundSession !== null && revokedSessions.has(boundSession)),
  };
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
    return;
  }
  const recorderStillHere =
    active?.sessionId === sessionId && active.recorder.state !== "inactive";
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
  if (recorderStillHere) {
    status = "recording";
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
  const updated = await updateMeta(database, sessionId, (latest) => ({
    ...persistRecorderLost(latest, Date.now()),
    needs_operator: true,
  }));
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

export async function startRecording(sessionId: string): Promise<void> {
  cancelRelease();
  if (revokedSessions.has(sessionId)) {
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
  let stream: MediaStream | null = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
    if (revokedSessions.has(sessionId)) {
      // Revoked while the permission prompt was open: never start capturing.
      stream.getTracks().forEach((track) => track.stop());
      status = "idle";
      emit();
      return;
    }
    const again = await readMeta(database, sessionId);
    if (again !== null) {
      stream.getTracks().forEach((track) => track.stop());
      canStart = false;
      meta = again;
      emit();
      return;
    }
    const mimeType = pickMimeType();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const created = emptyMeta(sessionId, Date.now());
    await writeMeta(database, created);
    meta = created;
    canStart = false;
    attachRecorder(sessionId, recorder, stream);
    recorder.start(2_000);
    status = "recording";
    ensurePump(sessionId);
    if (revokedSessions.has(sessionId)) {
      // Revoked during the meta write above: stop the take that just began.
      await interruptRecording(sessionId);
      return;
    }
  } catch (cause) {
    stream?.getTracks().forEach((track) => track.stop());
    const described = describeMediaError(cause);
    rememberHeldFailure(described.message);
    retryable = described.retryable;
    status = "error";
  }
  emit();
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
    const updated = await updateMeta(database, sessionId, (latest) =>
      persistInterrupted(latest, Date.now()),
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
  const updated = await updateMeta(database, sessionId, (latest) => {
    if (latest.recorder_state === "lost") {
      // A revocation declared the take lost before the recorder flushed its last slice.
      const kept = revokedSessions.has(sessionId) ? persistInterrupted(latest, now) : latest;
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
  });
  if (updated) {
    meta = updated;
  }
  return updated;
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
  revokedSessions.clear();
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    notePageHide();
  });
}

export function recordingView(): RecordingView {
  return view();
}
