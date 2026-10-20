// Persistent upload queue decisions (ADR-0005 §6.7, §7.2, §7.5, D9, D10).
//
// Sequence numbers are assigned here and stored with the chunk. Acknowledging
// or deleting a chunk must not change `next_sequence`. Finalize timing starts
// when the payload is persisted and is not moved by chunk retries.

export const FINALIZE_DELAY_MS = 30_000;
export const META_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
export const QUEUE_CAPACITY_BYTES = 500 * 1024 * 1024;
export const QUEUE_WARN_RATIO = 0.8;

export type RecorderState = "recording" | "stopped" | "lost";
export type ServerFinalizeState = "none" | "pending" | "complete" | "incomplete";

/** Permanent upload failures. The queue stops retrying these. */
export type UploadBlock =
  | "sequence_conflict"
  | "sequence_past_final"
  | "checksum"
  | "unauthorized"
  | "final_below_stored"
  | "invalid_finalize"
  | "finalize_conflict"
  | "recording_deleted";

export interface FinalizePayload {
  final_sequence: number;
  total_chunks: number;
  total_duration_ms: number;
  reason?: "recorder_lost";
}

export interface RecordingMeta {
  session_id: string;
  next_sequence: number;
  recorder_started_at: number | null;
  recorder_state: RecorderState;
  final_sequence?: number;
  finalize_pending: boolean;
  finalize_payload: FinalizePayload | null;
  finalize_confirmed: boolean;
  finalize_persisted_at: number | null;
  finalize_confirmed_at: number | null;
  finalize_attempt: number;
  finalize_next_attempt_at: number | null;
  server_finalize_state: ServerFinalizeState;
  last_chunk_end_ms: number;
  needs_operator: boolean;
  upload_block: UploadBlock | null;
}

export interface StoredChunkDraft {
  session_id: string;
  sequence: number;
  start_ms: number;
  end_ms: number;
  checksum: string;
  byte_length: number;
}

export interface ServerUploadStatus {
  finalize_state: ServerFinalizeState;
  recording_state: string;
  received_sequences: number[];
}

export type ReloadPlan =
  | { kind: "idle" }
  | { kind: "safe_to_close" }
  | { kind: "recorder_lost" }
  | { kind: "resume_uploads" };

export type CloseBanner =
  | { kind: "none" }
  | { kind: "safe" }
  | { kind: "confirming" }
  | { kind: "uploading"; pending: number }
  | { kind: "interrupted" }
  | { kind: "incomplete" }
  | { kind: "needs_operator"; block: UploadBlock | null };

export function emptyMeta(sessionId: string, now: number): RecordingMeta {
  return {
    session_id: sessionId,
    next_sequence: 0,
    recorder_started_at: now,
    recorder_state: "recording",
    finalize_pending: false,
    finalize_payload: null,
    finalize_confirmed: false,
    finalize_persisted_at: null,
    finalize_confirmed_at: null,
    finalize_attempt: 0,
    finalize_next_attempt_at: null,
    server_finalize_state: "none",
    last_chunk_end_ms: 0,
    needs_operator: false,
    upload_block: null,
  };
}

/** Read next_sequence and advance it. Callers persist meta and the chunk together. */
export function assignChunkSequence(meta: RecordingMeta): {
  sequence: number;
  meta: RecordingMeta;
} {
  return {
    sequence: meta.next_sequence,
    meta: { ...meta, next_sequence: meta.next_sequence + 1 },
  };
}

/**
 * An ACK removes the blob. It does not rewind or delete `next_sequence`.
 * The returned meta is the same counter the caller already holds.
 */
export function acknowledgeChunk(meta: RecordingMeta): RecordingMeta {
  return { ...meta, next_sequence: meta.next_sequence };
}

export function buildStoredChunk(
  meta: RecordingMeta,
  fields: { end_ms: number; checksum: string; byte_length: number },
): { sequence: number; meta: RecordingMeta; chunk: StoredChunkDraft } {
  const assigned = assignChunkSequence(meta);
  const next: RecordingMeta = {
    ...assigned.meta,
    last_chunk_end_ms: fields.end_ms,
  };
  return {
    sequence: assigned.sequence,
    meta: next,
    chunk: {
      session_id: meta.session_id,
      sequence: assigned.sequence,
      start_ms: meta.last_chunk_end_ms,
      end_ms: fields.end_ms,
      checksum: fields.checksum,
      byte_length: fields.byte_length,
    },
  };
}

/**
 * Send finalize when the queue is empty, or once 30s have passed since the
 * payload was persisted — whichever comes first. Chunk retries do not move
 * `finalize_persisted_at`. The 30s mark is not an upload timeout.
 */
export function finalizeDue(
  meta: RecordingMeta,
  pendingChunkCount: number,
  now: number,
): boolean {
  if (!meta.finalize_pending || meta.finalize_confirmed) {
    return false;
  }
  if (meta.finalize_payload == null || meta.finalize_persisted_at == null) {
    return false;
  }
  if (meta.server_finalize_state === "complete" || meta.server_finalize_state === "incomplete") {
    return false;
  }
  const deadlineReached = now >= meta.finalize_persisted_at + FINALIZE_DELAY_MS;
  const due = pendingChunkCount === 0 || deadlineReached;
  if (!due) {
    return false;
  }
  if (meta.finalize_next_attempt_at != null && now < meta.finalize_next_attempt_at) {
    return false;
  }
  return true;
}

/** Backoff for a failed finalize POST. The original deadline stays put. */
export function noteFinalizeAttempt(
  meta: RecordingMeta,
  now: number,
  failed: boolean,
): RecordingMeta {
  if (!failed) {
    return { ...meta, finalize_next_attempt_at: null };
  }
  const attempt = meta.finalize_attempt + 1;
  const delay = Math.min(30_000, 1_000 * 2 ** Math.min(attempt, 5));
  return {
    ...meta,
    finalize_attempt: attempt,
    finalize_next_attempt_at: now + delay,
  };
}

export function chunkRetryDelayMs(attempts: number): number {
  return Math.min(30_000, 1_000 * 2 ** Math.min(Math.max(attempts, 0), 5));
}

export function persistStop(meta: RecordingMeta, now: number): RecordingMeta {
  const finalSequence = meta.next_sequence - 1;
  return {
    ...meta,
    recorder_state: "stopped",
    final_sequence: finalSequence,
    finalize_pending: true,
    finalize_confirmed: false,
    finalize_persisted_at: now,
    server_finalize_state:
      meta.server_finalize_state === "none" ? "pending" : meta.server_finalize_state,
    finalize_payload: {
      final_sequence: finalSequence,
      total_chunks: meta.next_sequence,
      total_duration_ms: meta.last_chunk_end_ms,
    },
  };
}

export function persistRecorderLost(meta: RecordingMeta, now: number): RecordingMeta {
  const finalSequence = meta.next_sequence - 1;
  return {
    ...meta,
    recorder_state: "lost",
    final_sequence: finalSequence,
    finalize_pending: true,
    finalize_confirmed: false,
    finalize_persisted_at: meta.finalize_persisted_at ?? now,
    server_finalize_state:
      meta.server_finalize_state === "none" ? "pending" : meta.server_finalize_state,
    finalize_payload: {
      final_sequence: finalSequence,
      total_chunks: meta.next_sequence,
      total_duration_ms: meta.last_chunk_end_ms,
      reason: "recorder_lost",
    },
  };
}

/** Reload resumes uploads or declares the take lost. It never starts a recorder. */
export function planReload(meta: RecordingMeta | null): ReloadPlan {
  if (meta === null) {
    return { kind: "idle" };
  }
  if (meta.finalize_confirmed && meta.server_finalize_state === "complete") {
    return { kind: "safe_to_close" };
  }
  if (meta.recorder_state === "recording") {
    return { kind: "recorder_lost" };
  }
  return { kind: "resume_uploads" };
}

export function shouldDiscardMeta(meta: RecordingMeta, now: number): boolean {
  if (meta.recorder_state === "lost") {
    return false;
  }
  if (!meta.finalize_confirmed || meta.server_finalize_state !== "complete") {
    return false;
  }
  if (meta.finalize_confirmed_at == null) {
    return false;
  }
  return now >= meta.finalize_confirmed_at + META_RETENTION_MS;
}

export function sequencesAlreadyReceived(local: number[], received: number[]): number[] {
  const have = new Set(received);
  return local.filter((sequence) => have.has(sequence));
}

/**
 * Merge a server status onto `meta`.
 *
 * Only `server_finalize_state`, `finalize_confirmed`, `finalize_pending`, and
 * `finalize_confirmed_at` change. `next_sequence`, `recorder_state`, and
 * `finalize_payload` are copied through so a caller that applies this to the
 * latest row cannot rewind them. Callers must pass the row just read inside
 * the same write transaction — never a snapshot taken before an await.
 */
export function applyServerStatus(
  meta: RecordingMeta,
  status: ServerUploadStatus,
  now: number,
): RecordingMeta {
  // A status read that started before finalize returned must not walk a
  // terminal server state back to pending or none.
  const held = meta.server_finalize_state;
  const incoming = status.finalize_state;
  const heldTerminal = held === "complete" || held === "incomplete";
  const incomingTerminal = incoming === "complete" || incoming === "incomplete";
  const finalizeState = heldTerminal && !incomingTerminal ? held : incoming;
  const next: RecordingMeta = {
    ...meta,
    server_finalize_state: finalizeState,
  };
  if (finalizeState === "complete") {
    next.finalize_confirmed = true;
    next.finalize_pending = false;
    next.finalize_confirmed_at = meta.finalize_confirmed_at ?? now;
  } else if (finalizeState === "incomplete") {
    next.finalize_confirmed = false;
    next.finalize_pending = false;
  }
  return next;
}

/** 409/400/401/422 that will never succeed if retried as-is. */
export function permanentUploadBlock(status: number, code: string | null): UploadBlock | null {
  if (status === 401) {
    return "unauthorized";
  }
  if (status === 409 && code === "sequence_past_final") {
    return "sequence_past_final";
  }
  if (status === 409 && code === "final_sequence_below_stored") {
    return "final_below_stored";
  }
  if (status === 409 && code === "sequence_conflict") {
    return "sequence_conflict";
  }
  if (status === 409 && code === "finalize_conflict") {
    return "finalize_conflict";
  }
  if (status === 409 && code === "recording_deleted") {
    return "recording_deleted";
  }
  if (status === 409) {
    return "sequence_conflict";
  }
  if (status === 400 && (code === "checksum_mismatch" || code === "checksum")) {
    return "checksum";
  }
  if (status === 400 || status === 422) {
    return "invalid_finalize";
  }
  return null;
}

/** Auth and finalize failures must not be posted again. A bad chunk must not block the rest. */
export function stopsFinalize(block: UploadBlock): boolean {
  return (
    block === "unauthorized" ||
    block === "final_below_stored" ||
    block === "invalid_finalize" ||
    block === "finalize_conflict" ||
    block === "recording_deleted"
  );
}

export function notePermanentFailure(meta: RecordingMeta, block: UploadBlock): RecordingMeta {
  return {
    ...meta,
    needs_operator: true,
    upload_block: block,
    finalize_pending: stopsFinalize(block) ? false : meta.finalize_pending,
    finalize_next_attempt_at: stopsFinalize(block) ? null : meta.finalize_next_attempt_at,
  };
}

export function permanentBlockMessage(block: UploadBlock | null): string {
  switch (block) {
    case "sequence_conflict":
      return "片段與伺服器已保存的內容衝突，請通知管理員";
    case "sequence_past_final":
      return "片段序號已超過結束範圍，請通知管理員";
    case "checksum":
      return "片段校驗失敗，無法上傳，請通知管理員";
    case "unauthorized":
      return "補傳時間已結束，請通知老師或管理員";
    case "final_below_stored":
      return "結束序號小於伺服器已收到的片段，請通知管理員";
    case "invalid_finalize":
      return "結束標記與伺服器的錄影不一致，請通知管理員";
    case "finalize_conflict":
      return "結束標記與伺服器已保存的內容衝突，請通知管理員";
    case "recording_deleted":
      return "這段錄影已刪除，無法繼續上傳，請通知管理員";
    default:
      return "上傳無法繼續，請通知老師或管理員";
  }
}

export function storageWarning(totalBytes: number): string | null {
  if (totalBytes < QUEUE_CAPACITY_BYTES * QUEUE_WARN_RATIO) {
    return null;
  }
  const used = Math.round(totalBytes / (1024 * 1024));
  return `錄影暫存接近上限（已使用 ${used} MB / 500 MB）`;
}

export function closeBanner(meta: RecordingMeta | null, pending: number): CloseBanner {
  if (meta === null) {
    return { kind: "none" };
  }
  if (meta.finalize_confirmed && meta.server_finalize_state === "complete") {
    return { kind: "safe" };
  }
  if (meta.recorder_state === "lost") {
    return { kind: "interrupted" };
  }
  if (meta.needs_operator) {
    return { kind: "needs_operator", block: meta.upload_block };
  }
  if (meta.server_finalize_state === "incomplete") {
    return { kind: "incomplete" };
  }
  if (pending > 0 && meta.recorder_state !== "recording") {
    return { kind: "uploading", pending };
  }
  if (meta.finalize_pending) {
    return { kind: "confirming" };
  }
  return { kind: "none" };
}

/** In-memory stand-in for the IndexedDB transaction: assign, store, then ACK. */
export class MemoryUploadLog {
  meta: RecordingMeta;
  chunks = new Map<number, StoredChunkDraft>();

  constructor(sessionId: string, now = 0) {
    this.meta = emptyMeta(sessionId, now);
  }

  put(endMs: number): number {
    const built = buildStoredChunk(this.meta, {
      end_ms: endMs,
      checksum: "abc",
      byte_length: 8,
    });
    this.meta = built.meta;
    this.chunks.set(built.sequence, built.chunk);
    return built.sequence;
  }

  ack(sequence: number): void {
    this.chunks.delete(sequence);
    this.meta = acknowledgeChunk(this.meta);
  }
}
