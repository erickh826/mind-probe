// Browser side of the upload queue. IndexedDB is the source of truth.
// A reload calls `bindSession`, which re-uploads pending slices and a pending
// finalize. It does not call `MediaRecorder.start`.

import { ApiError, fetchUploadStatus, finalizeRecording, uploadRecordingChunk } from "./api";
import {
  deleteChunk,
  listChunks,
  openRecordingDb,
  purgeExpiredMeta,
  readMeta,
  storeChunkAndBumpSequence,
  writeChunk,
  writeMeta,
  type PersistedChunk,
} from "./recordingDb";
import {
  applyServerStatus,
  chunkRetryDelayMs,
  closeBanner,
  emptyMeta,
  finalizeDue,
  noteFinalizeAttempt,
  persistRecorderLost,
  persistStop,
  planReload,
  sequencesAlreadyReceived,
  storageWarning,
  type CloseBanner,
  type RecordingMeta,
} from "./uploadQueue";
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

let databasePromise: Promise<IDBDatabase> | null = null;
let active: { sessionId: string; recorder: MediaRecorder; stream: MediaStream } | null = null;
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
  failedCount = chunks.filter((chunk) => chunk.attempts > 0 && chunk.next_attempt_at > Date.now()).length;
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
  try {
    const status = await fetchUploadStatus(sessionId);
    const chunks = await listChunks(database, sessionId);
    for (const sequence of sequencesAlreadyReceived(
      chunks.map((chunk) => chunk.sequence),
      status.received_sequences,
    )) {
      await deleteChunk(database, sessionId, sequence);
    }
    current = applyServerStatus(current, status, now);
    await writeMeta(database, current);
  } catch (cause) {
    errorMessage = `無法讀取上傳狀態：${cause instanceof Error ? cause.message : String(cause)}`;
  }
  const recorderStillHere =
    active?.sessionId === sessionId && active.recorder.state !== "inactive";
  const plan = planReload(current);
  // A remount while MediaRecorder is still running is not a lost take.
  // recorder_lost is only for a reload where the original recorder is gone.
  if (plan.kind === "recorder_lost" && !recorderStillHere) {
    current = persistRecorderLost(current, now);
    await writeMeta(database, current);
  }
  meta = current;
  canStart = false;
  if (recorderStillHere) {
    status = "recording";
  } else if (plan.kind !== "idle") {
    status = "stopped";
  }
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

async function persistBlob(sessionId: string, blob: Blob): Promise<void> {
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
    status = "stopped";
    stream.getTracks().forEach((track) => track.stop());
    if (active?.recorder === recorder) {
      active = null;
    }
    void persistChain
      .then(async () => {
        const database = await db();
        const current = await readMeta(database, sessionId);
        if (current === null || current.recorder_state === "lost") {
          return;
        }
        if (current.finalize_pending && current.finalize_persisted_at != null) {
          meta = current;
          return;
        }
        const stopped = persistStop(current, Date.now());
        await writeMeta(database, stopped);
        meta = stopped;
      })
      .catch((cause: unknown) => {
        errorMessage = `無法保存結束標記：${String(cause)}`;
      })
      .finally(() => emit());
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
  active?.stream.getTracks().forEach((track) => track.stop());
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
    if (chunk.attempts > 0) {
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
      (chunk) => !inFlight.has(chunk.sequence) && now >= chunk.next_attempt_at,
    );
    if (next) {
      inFlight.add(next.sequence);
      try {
        await uploadRecordingChunk(sessionId, next.sequence, next.blob, {
          start_ms: next.start_ms,
          end_ms: next.end_ms,
          checksum: next.checksum,
        });
        await deleteChunk(database, sessionId, next.sequence);
        uploadedCount += 1;
        chunkUploaded?.(next.sequence, next.end_ms);
        if (current) {
          try {
            const serverStatus = await fetchUploadStatus(sessionId);
            current = applyServerStatus(current, serverStatus, Date.now());
            await writeMeta(database, current);
            meta = current;
          } catch {
            // The blob is gone locally. The next finalize or status read will catch up.
          }
        }
      } catch (cause) {
        const attempts = next.attempts + 1;
        await writeChunk(database, {
          ...next,
          attempts,
          next_attempt_at: Date.now() + chunkRetryDelayMs(attempts),
        });
        failedCount = (await listChunks(database, sessionId)).filter((chunk) => chunk.attempts > 0).length;
        errorMessage =
          cause instanceof ApiError
            ? `片段 ${next.sequence} 上傳失敗：${cause.message}`
            : `片段 ${next.sequence} 上傳失敗：${String(cause)}`;
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
        const result = await finalizeRecording(sessionId, payload);
        const stillOpen = result.finalize_state === "pending" || result.finalize_state === "none";
        const updated = applyServerStatus(
          noteFinalizeAttempt(current, Date.now(), stillOpen),
          {
            finalize_state: result.finalize_state,
            recording_state: result.recording_state,
            received_sequences: [],
          },
          Date.now(),
        );
        await writeMeta(database, updated);
        meta = updated;
        errorMessage = null;
      } catch (cause) {
        // Chunk retries must not move finalize_persisted_at. Only the attempt clock moves.
        const updated = noteFinalizeAttempt(current, Date.now(), true);
        await writeMeta(database, updated);
        meta = updated;
        errorMessage = `結束標記送出失敗：${cause instanceof Error ? cause.message : String(cause)}`;
      }
    }
  } catch (cause) {
    errorMessage = `上傳佇列錯誤：${cause instanceof Error ? cause.message : String(cause)}`;
  } finally {
    pumping = false;
    emit();
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    if (active && active.recorder.state !== "inactive") {
      active.recorder.stop();
    }
  });
}

export function recordingView(): RecordingView {
  return view();
}
