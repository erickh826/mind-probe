// Event envelope schema (spec §9 標準事件封套).
// Mirrors server/app/events.py — keep the two in sync.
//
// The payload contracts below are governed by
// docs/adr/0001-websocket-architecture.md §5/§7 (short-lived commands, ACK
// de-duplication), docs/adr/0003-time-sync.md §7 (offset versioning) and
// docs/adr/0004-cross-cutting-rules.md §1/§2 (snapshot_revision, pause reason).

/**
 * Origin of an event. Mirrors `Source` in server/app/events.py.
 *
 * Deliberately narrower than {@link Role}: the server rejects events emitted
 * by `teacher`/`observer` connections with `read_only_role_cannot_emit_events`
 * (server/app/websocket.py, `EVENT_SOURCE_ROLES`).
 */
export const SOURCE_VALUES = ["student", "wizard", "server"] as const;

export type Source = (typeof SOURCE_VALUES)[number];

/**
 * Connection role used in the WebSocket URL path (`/ws/{session_id}/{role}`)
 * and in the control messages in ./messages. Mirrors `VALID_ROLES` in
 * server/app/websocket.py: `student`/`wizard` allow one active connection
 * each, `teacher`/`observer` allow multiple read-only connections
 * (ADR-0001 §9).
 */
export const ROLE_VALUES = ["student", "wizard", "teacher", "observer"] as const;

export type Role = (typeof ROLE_VALUES)[number];

export const EVENT_TYPES = [
  // Session lifecycle
  "session_created",
  "session_started",
  "session_paused",
  "session_resumed",
  "session_ended",
  // Clock sync (§9)
  "ping",
  "pong",
  "clock_sync",
  // Clip playback (§7)
  "clip_command",
  "clip_started",
  "clip_ended",
  "clip_interrupted",
  "return_to_idle",
  "thinking_started",
  // Wizard annotations
  "fallback_used",
  "uncovered_question",
  "operator_error",
  "observer_note",
  // Recording (§10)
  "recording_started",
  "recording_stopped",
  "chunk_uploaded",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/**
 * Reason a session was paused (ADR-0004 §2). Mirrors `PAUSE_REASONS` in
 * server/app/websocket.py — a `session_paused` event carrying any other value
 * is rejected with `invalid_pause_reason`.
 *
 * Technical pauses (everything except `manual`) are excluded from student
 * scoring metrics (ADR-0002 §6, §8).
 */
export const PAUSE_REASONS = [
  "manual",
  "network_failure",
  "clock_sync_failure",
  "recording_failure",
  "ethical_or_safety_stop",
] as const;

export type PauseReason = (typeof PAUSE_REASONS)[number];

/**
 * Event types the server treats as short-lived commands (ADR-0001 §5).
 * Mirrors `SHORT_LIVED_COMMAND_TYPES` in server/app/websocket.py: their
 * payloads MUST satisfy {@link ShortLivedCommandPayload}, otherwise the
 * server replies with an `event_ack` of `rejected` / `missing_command_fields`.
 */
export const SHORT_LIVED_COMMAND_EVENT_TYPES = [
  "clip_command",
  "return_to_idle",
  "session_paused",
] as const;

export type ShortLivedCommandEventType =
  (typeof SHORT_LIVED_COMMAND_EVENT_TYPES)[number];

/**
 * The four fixed semantic fallback categories (ADR-0002 §2). Mirrors
 * `FALLBACK_CATEGORIES` in server/app/cases.py.
 *
 * The Wizard picks a *category*, never a clip id: which approved clip
 * expresses that category for the current case state is a server decision
 * (`cases.resolve_fallback_clip`), so two students asking the same
 * unanswerable question get the same auditable response. A
 * `request_fallback` carrying anything else is rejected with
 * `invalid_fallback_category`.
 */
export const FALLBACK_CATEGORIES = [
  "CLARIFY",
  "ONE_AT_A_TIME",
  "UNKNOWN_OR_UNSURE",
  "DECLINE_OR_BOUNDARY",
] as const;

export type FallbackCategory = (typeof FALLBACK_CATEGORIES)[number];

/**
 * The Wizard-supplied fallback reasons of ADR-0002 §7. Mirrors
 * `FALLBACK_REASONS` in server/app/cases.py, including its order.
 *
 * This is the picklist the Wizard UI should offer, **not** a closed enum: the
 * ADR calls them 可選 (available) reasons and the server records whatever
 * string arrives rather than refusing a live fallback over a taxonomy value.
 * See {@link FallbackReasonValue} for the type that models that openness.
 */
export const FALLBACK_REASONS = [
  "no_matching_response",
  "ambiguous_question",
  "compound_question",
  "outside_character_knowledge",
  "disclosure_not_allowed",
  "wizard_could_not_find_clip",
  "technical_delay",
] as const;

export type FallbackReason = (typeof FALLBACK_REASONS)[number];

/**
 * A reason as it travels on the wire: one of {@link FALLBACK_REASONS}, or any
 * other string the Wizard supplied. `string & {}` keeps the known literals in
 * editor autocomplete instead of letting the union collapse to plain `string`.
 */
export type FallbackReasonValue = FallbackReason | (string & {});

/**
 * Fields the server injects into the `payload` of every accepted event
 * (ADR-0003 §7). Clients never send these; they appear on events broadcast
 * back out. Historical events are never recalculated, so the values record
 * the offset that was in effect when the event was accepted.
 */
export interface ServerAnnotatedFields {
  /** Version of the connection's clock offset used for this event. */
  clock_offset_version?: number;
  /** `client_timestamp_ms + clock_offset_ms`, computed server-side. */
  normalized_server_timestamp_ms?: number;
}

/** Common base for every event payload. */
export interface BaseEventPayload extends ServerAnnotatedFields {
  /**
   * Optional client-generated idempotency key for ACK de-duplication
   * (ADR-0001 §7). When absent the server falls back to
   * `payload.command_id`, then to `session_id:source:sequence`.
   */
  event_id?: string;
}

/**
 * Payload contract for short-lived commands (ADR-0001 §5, ADR-0004 §1).
 * The server discards such a command — it is never forwarded — once
 * `expires_at_ms` has passed or `snapshot_revision` is older than the
 * server's current session snapshot revision.
 */
export interface ShortLivedCommandPayload extends BaseEventPayload {
  command_id: string;
  /** Deadline in **server monotonic** ms (ADR-0003 §1). */
  expires_at_ms: number;
  /** Snapshot revision this command was issued against. */
  snapshot_revision: number;
}

/**
 * Standard event envelope (spec §9). Event-type-specific fields live in
 * `payload` (審閱後修訂 2), e.g. clip_interrupted:
 *   { clip_id, played_duration_ms, reason }
 *
 * For transport-level code with an unnarrowed payload use
 * {@link AnyEventEnvelope}, not the bare default: the named payload
 * interfaces below are not assignable to `Record<string, unknown>` because
 * TypeScript withholds implicit index signatures from interfaces.
 */
export interface EventEnvelope<P extends object = Record<string, unknown>> {
  session_id: string;
  sequence: number;
  client_timestamp_ms: number;
  server_timestamp_ms?: number | null;
  clock_offset_ms?: number | null;
  source: Source;
  event_type: EventType;
  payload: P;
}

/**
 * An envelope whose payload has not (yet) been narrowed to a specific event
 * type — useful for transport-level code such as the WebSocket router.
 *
 * Note this is *not* `EventEnvelope<Record<string, unknown>>`: TypeScript
 * gives implicit index signatures to type aliases but not to interfaces, so
 * the payload interfaces below would not be assignable to it.
 */
export type AnyEventEnvelope = EventEnvelope<BaseEventPayload>;

// --- Event payload shapes (non-exhaustive) ---------------------------------

/** `clip_command`: wizard asks the server to play a clip (spec §7). */
export interface ClipCommandPayload extends ShortLivedCommandPayload {
  clip_id: string;
}

/** `return_to_idle`: stop the active clip and return to the idle loop. */
export type ReturnToIdlePayload = ShortLivedCommandPayload;

/**
 * `session_paused`: a short-lived command that must additionally record its
 * reason (ADR-0004 §2) — the server validates both halves.
 *
 * `lost_roles`/`auto_paused` are optional here so that code handling a
 * `session_paused` event without first discriminating on `source` can still
 * read them. They are only ever set by the server's automatic network-failure
 * pause, which {@link AutoSessionPausedPayload} describes precisely — prefer
 * that type once the server-authored case has been narrowed.
 */
export interface SessionPausedPayload extends ShortLivedCommandPayload {
  reason: PauseReason;
  /** Roles whose loss triggered an automatic pause (ADR-0002 §8). */
  lost_roles?: string[];
  /** True when the server, not a Wizard, issued the pause. */
  auto_paused?: boolean;
}

/**
 * `session_paused` as authored by the *server* when a critical
 * single-connection role (`student`/`wizard`) stays gone past ADR-0002 §8's
 * 2s grace window (`_pause_for_network_failure` in
 * server/app/websocket.py).
 *
 * A separate shape from {@link SessionPausedPayload} because no client
 * command stands behind it: the server-authored envelope carries no
 * `command_id`/`expires_at_ms`/`snapshot_revision`, and always carries all
 * three fields below. `auto_paused` is the literal `true`, so it also
 * discriminates the two shapes. The pause is technical, so it is excluded
 * from student scoring (ADR-0002 §6, §8).
 */
export interface AutoSessionPausedPayload extends BaseEventPayload {
  reason: PauseReason;
  /** Non-empty: the critical roles that dropped. */
  lost_roles: string[];
  auto_paused: true;
}

export interface ClipStartedPayload extends BaseEventPayload {
  clip_id: string;
}

export interface ClipEndedPayload extends BaseEventPayload {
  clip_id: string;
}

export interface ClipInterruptedPayload extends BaseEventPayload {
  clip_id: string;
  played_duration_ms: number;
  /** e.g. "student_barge_in" (spec §7.2). */
  reason: string;
}

/**
 * `fallback_used`: the server's record of a resolved fallback (ADR-0002 §7).
 *
 * Never authored by a client — a client-sent `fallback_used` envelope is
 * rejected with `fallback_is_server_resolved`. The server builds this payload
 * in `_handle_request_fallback` by copying the Wizard's
 * `RequestFallbackPayload` (see ./messages) and overwriting the fields below,
 * then broadcasts it to *every* role; that is how the requesting Wizard
 * learns `resolved_clip_id`. The echoed short-lived command
 * fields (`command_id`, `expires_at_ms`, `snapshot_revision`) therefore ride
 * along on the wire, but they are request bookkeeping rather than part of the
 * event contract, so they are not declared here.
 */
export interface FallbackUsedPayload extends BaseEventPayload {
  fallback_category: FallbackCategory;
  /** Clip the server chose for this category (`cases.resolve_fallback_clip`). */
  resolved_clip_id: string;
  /**
   * Always present, `null` when the Wizard supplied no reason — the server
   * writes the key unconditionally.
   */
  reason: FallbackReasonValue | null;
  /** Always `true` in the initial version: fallbacks are Wizard-triggered. */
  wizard_selected: boolean;
  /** Fallback streak *including* this one (ADR-0002 §5). */
  consecutive_fallbacks: number;
  /** Optional link back to the student question segment (ADR-0002 §7). */
  question_segment_id?: string;
}

/**
 * `uncovered_question`: the Wizard's `M` hotkey marking a question the clip
 * library cannot answer (spec §8, ADR-0002 §7).
 *
 * Wizard-only — the server rejects other sources with
 * `uncovered_question_requires_wizard` — and broadcast to the review roles
 * (`teacher`/`observer`) only. The server persists the payload verbatim
 * without validating its shape, so every field is optional; treat this as the
 * agreed annotation vocabulary rather than an enforced schema.
 */
export interface UncoveredQuestionPayload extends BaseEventPayload {
  /** Verbatim (or paraphrased) student question. */
  question_text?: string;
  /** Free-form Wizard note. */
  note?: string;
  /** Session-relative ms at which the question was asked. */
  timestamp_ms?: number;
  /** Optional link to the student question segment (ADR-0002 §7). */
  question_segment_id?: string;
}
