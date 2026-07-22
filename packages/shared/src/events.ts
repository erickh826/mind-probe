// Event envelope schema (spec §9 標準事件封套).
// Mirrors server/app/events.py — keep the two in sync.

export type Source = "student" | "wizard" | "server";

export type EventType =
  // Session lifecycle
  | "session_created"
  | "session_started"
  | "session_paused"
  | "session_resumed"
  | "session_ended"
  // Clock sync (§9)
  | "ping"
  | "pong"
  | "clock_sync"
  // Clip playback (§7)
  | "clip_command"
  | "clip_started"
  | "clip_ended"
  | "clip_interrupted"
  | "return_to_idle"
  | "thinking_started"
  // Wizard annotations
  | "fallback_used"
  | "uncovered_question"
  | "operator_error"
  | "observer_note"
  // Recording (§10)
  | "recording_started"
  | "recording_stopped"
  | "chunk_uploaded";

/**
 * Standard event envelope (spec §9). Event-type-specific fields live in
 * `payload` (審閱後修訂 2), e.g. clip_interrupted:
 *   { clip_id, played_duration_ms, reason }
 */
export interface EventEnvelope<P extends Record<string, unknown> = Record<string, unknown>> {
  session_id: string;
  sequence: number;
  client_timestamp_ms: number;
  server_timestamp_ms?: number | null;
  clock_offset_ms?: number | null;
  source: Source;
  event_type: EventType;
  payload: P;
}

// Example payload shapes (non-exhaustive).
export interface ClipStartedPayload {
  clip_id: string;
}

export interface ClipInterruptedPayload {
  clip_id: string;
  played_duration_ms: number;
  reason: string;
}
