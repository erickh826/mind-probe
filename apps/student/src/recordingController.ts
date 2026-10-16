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
  closeBanner,
  emptyMeta,
  finalizeDue,
  noteFinalizeAttempt,
  notePermanentFailure,
  permanentBlockMessage,
  permanentUploadBlock,
  persistRecorderLost,
  persistStop,
  planReload,
  sequencesAlreadyReceived,
  storageWarning,
  type CloseBanner,
  type FinalizePayload,
  type RecordingMeta,
  type ServerUploadStatus,
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
let pumpTimer: number | null = null;
let pumping = false;
let boundSession: string | null = null;
let meta: RecordingMeta | null = null;
let pendingCount = 0;
let uploadedCount = 0;
let failedCount = 0;
let errorMessage: string | null = null;
let warning: string | null = null;
let canStart = true;
let retryable = false;
let status: RecorderStatus = typeof MediaRecorder === "undefined" ? "unavailable" : "idle";
const listeners = new Set<Listener>();
const inFlight = new Set<number>();
let chunkUploaded: ((sequence: number, timestampMs: number) => void) | null = null;
let persistChain: Promise<void> = Promise.resolve();

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
    canStart,
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
  const tick = () => {
    pumpTimer = window.setTimeout(() => {
      void pump(sessionId).finally(() => {
        if (boundSession === sessionId) {
          tick();
        } else {
          pumpTimer = null;
        }
      });
    }, 400);
  };
  void pump(sessionId).finally(tick);
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
    errorMessage = `無法讀取上傳狀態：${cause instanceof Error ? cause.message : String(cause)}`;
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

export async function bindSession(sessionId: string): Promise<void> {
  cancelRelease();
  if (boundSession !== sessionId) {
    if (pumpTimer !== null) {
      window.clearTimeout(pumpTimer);
      pumpTimer = null;
    }
    uploadedCount = 0;
    failedCount = 0;
    pendingCount = 0;
    errorMessage = null;
  }
  boundSession = sessionId;
  try {
    await reconcile(sessionId, Date.now());
    await refreshPending(sessionId);
    ensurePump(sessionId);
  } catch (cause) {
    errorMessage = `錄影佇列無法開啟：${cause instanceof Error ? cause.message : String(cause)}`;
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

function attachRecorder(sessionId: string, recorder: MediaRecorder, stream: MediaStream): void {
  active = { sessionId, recorder, stream };
  persistChain = Promise.resolve();
  recorder.ondataavailable = (event) => {
    const blob = event.data;
    persistChain = persistChain
      .then(() => persistBlob(sessionId, blob))
      .catch((cause: unknown) => {
        errorMessage = `無法保存錄影片段：${String(cause)}`;
        emit();
      });
  };
  recorder.onerror = () => {
    errorMessage = "MediaRecorder error.";
    status = "error";
    emit();
  };
  recorder.onstop = () => {
    void handleRecorderStop(sessionId, stream);
  };
}

export async function startRecording(sessionId: string): Promise<void> {
  cancelRelease();
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
    errorMessage = "MediaRecorder is not available in this browser.";
    emit();
    return;
  }
  status = "requesting";
  errorMessage = null;
  retryable = false;
  emit();
  let stream: MediaStream | null = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
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
  } catch (cause) {
    stream?.getTracks().forEach((track) => track.stop());
    const described = describeMediaError(cause);
    errorMessage = described.message;
    retryable = described.retryable;
    status = "error";
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

async function pump(sessionId: string): Promise<void> {
  if (pumping || boundSession !== sessionId) {
    return;
  }
  pumping = true;
  try {
    const database = await db();
    let current = await readMeta(database, sessionId);
    if (current && (await purgeExpiredMeta(database, current, Date.now()))) {
      meta = null;
      pendingCount = 0;
      return;
    }
    const chunks = await refreshPending(sessionId);
    meta = current;
    const now = Date.now();
    const next = chunks.find(
      (chunk) => !chunk.terminal && !inFlight.has(chunk.sequence) && now >= chunk.next_attempt_at,
    );
    if (next) {
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
        try {
          const serverStatus = await transport.fetchUploadStatus(sessionId);
          const updated = await updateMeta(database, sessionId, (latest) =>
            applyServerStatus(latest, serverStatus, Date.now()),
          );
          if (updated) {
            meta = updated;
          }
        } catch {
          // The blob is gone locally. The next finalize or status read will catch up.
        }
      } catch (cause) {
        const block = cause instanceof ApiError ? permanentUploadBlock(cause.status, cause.code) : null;
        if (block) {
          await writeChunk(database, {
            ...next,
            attempts: next.attempts + 1,
            terminal: true,
            next_attempt_at: Number.MAX_SAFE_INTEGER,
          });
          const updated = await updateMeta(database, sessionId, (latest) =>
            notePermanentFailure(latest, block),
          );
          if (updated) {
            meta = updated;
          }
          errorMessage = permanentBlockMessage(block);
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
          errorMessage =
            cause instanceof ApiError
              ? `片段 ${next.sequence} 上傳失敗：${cause.message}`
              : `片段 ${next.sequence} 上傳失敗：${String(cause)}`;
        }
      } finally {
        inFlight.delete(next.sequence);
      }
    }
    current = await readMeta(database, sessionId);
    const pending = await refreshPending(sessionId);
    meta = current;
    if (current && current.finalize_payload && finalizeDue(current, pending.length, Date.now())) {
      const payload = current.finalize_payload;
      try {
        const result = await transport.finalizeRecording(sessionId, payload);
        const stillOpen = result.finalize_state === "pending" || result.finalize_state === "none";
        const updated = await updateMeta(database, sessionId, (latest) => {
          // A stop that landed during the POST owns the payload. Do not confirm
          // a declaration the server has not seen.
          if (!sameFinalizePayload(latest.finalize_payload, payload)) {
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
        errorMessage = null;
      } catch (cause) {
        const block = cause instanceof ApiError ? permanentUploadBlock(cause.status, cause.code) : null;
        // Chunk retries must not move finalize_persisted_at. Only the attempt clock moves.
        const updated = await updateMeta(database, sessionId, (latest) =>
          block ? notePermanentFailure(latest, block) : noteFinalizeAttempt(latest, Date.now(), true),
        );
        if (updated) {
          meta = updated;
        }
        errorMessage = block
          ? permanentBlockMessage(block)
          : `結束標記送出失敗：${cause instanceof Error ? cause.message : String(cause)}`;
      }
    }
  } catch (cause) {
    errorMessage = `上傳佇列錯誤：${cause instanceof Error ? cause.message : String(cause)}`;
  } finally {
    pumping = false;
    emit();
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
  const lost = stopAsLost;
  stopAsLost = false;
  const database = await db();
  const updated = await updateMeta(database, sessionId, (latest) => {
    if (latest.recorder_state === "lost") {
      return latest;
    }
    if (!lost && latest.finalize_pending && latest.finalize_persisted_at != null) {
      return latest;
    }
    return lost ? persistRecorderLost(latest, now) : persistStop(latest, now);
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
      errorMessage = `無法保存結束標記：${String(cause)}`;
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

export async function pumpSession(sessionId: string): Promise<void> {
  boundSession = sessionId;
  await pump(sessionId);
}

export async function resetRecordingControllerForTests(): Promise<void> {
  if (pumpTimer !== null && typeof window !== "undefined") {
    window.clearTimeout(pumpTimer);
  }
  pumpTimer = null;
  const pending = databasePromise;
  databasePromise = null;
  if (pending) {
    try {
      (await pending).close();
    } catch {
      // The database may already be closed.
    }
  }
  if (typeof indexedDB !== "undefined") {
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
  pumping = false;
  boundSession = null;
  meta = null;
  pendingCount = 0;
  uploadedCount = 0;
  failedCount = 0;
  errorMessage = null;
  warning = null;
  canStart = true;
  retryable = false;
  status = typeof MediaRecorder === "undefined" ? "unavailable" : "idle";
  inFlight.clear();
  persistChain = Promise.resolve();
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    notePageHide();
  });
}

export function recordingView(): RecordingView {
  return view();
}
