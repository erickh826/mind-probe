// WebSocket control-message schema (ADR-0001, ADR-0003, ADR-0004).
// Mirrors the message shapes emitted/consumed by server/app/websocket.py —
// keep the two in sync.
//
// Control messages are distinguished from `EventEnvelope` by key presence,
// not by value: control messages carry a top-level `type`, envelopes carry
// `event_type` (see `_handle_message` in server/app/websocket.py). This
// matters because `EventType` also contains "ping"/"pong"/"clock_sync" as
// legacy envelope event types reserved for later ADR-0002 work, so keying a
// guard on `type === "ping"` alone would be ambiguous.

import { EVENT_TYPES, FALLBACK_CATEGORIES, ROLE_VALUES, SOURCE_VALUES } from "./events";
import type {
  AnyEventEnvelope,
  FallbackCategory,
  FallbackReasonValue,
  Role,
  ShortLivedCommandPayload,
} from "./events";
import type { CharacterState } from "./clip";

// --- Heartbeat (ADR-0001 §2) -----------------------------------------------

/** Client -> server. 10s interval, 5s pong wait, 3 misses => offline. */
export interface PingMessage {
  type: "ping";
  ping_id: string;
  client_timestamp_ms: number;
}

/** Server -> client. `ping_id` is echoed back unvalidated. */
export interface PongMessage {
  type: "pong";
  ping_id: string | null;
  /** Server wall-clock ms. */
  server_timestamp_ms: number;
}

// --- Event ACK (ADR-0001 §7) -----------------------------------------------

export type EventAckStatus = "accepted" | "duplicate" | "rejected";

/**
 * Server -> client acknowledgement of a submitted event envelope.
 *
 * `event_id` is the server's de-duplication key (`payload.event_id`,
 * `payload.command_id`, or `session_id:source:sequence`), and is `null` when
 * the message could not be parsed far enough to derive one. The server always
 * sends the `reason` key, using `null` for accepted/duplicate acks.
 */
export interface EventAckMessage {
  type: "event_ack";
  event_id: string | null;
  status: EventAckStatus;
  reason?: string | null;
}

// --- Snapshot-first reconnect (ADR-0001 §4, ADR-0004 §1) -------------------

/** Client -> server, sent immediately after a reconnect handshake. */
export interface ReconnectHelloMessage {
  type: "reconnect_hello";
  session_id: string;
  role: Role;
  last_snapshot_revision: number;
  last_acked_sequence: number;
  /** Events the client has not yet seen an `event_ack` for. */
  pending_event_ids: string[];
}

export type SessionState = "created" | "starting" | "active" | "paused" | "ended";

export type PlaybackState = "idle" | "playing";

/**
 * Server -> client authoritative session state. The client applies this
 * before resending anything from `pending_event_ids`, which then flow through
 * the normal de-duplicated event path.
 *
 * `start_at_server_ms` is present only while `session_state` is `"starting"`
 * (the server omits the key otherwise).
 */
export interface SessionSnapshotMessage {
  type: "session_snapshot";
  /** Incremented on every server-side state change (ADR-0004 §1). */
  snapshot_revision: number;
  session_state: SessionState;
  playback_state: PlaybackState;
  active_clip: string | null;
  character_state: CharacterState | string;
  cooperation_level: number;
  revealed_facts: string[];
  /**
   * Fallback streak since the last ordinary clip (ADR-0002 §5). Carried in the
   * snapshot so a reconnecting Wizard restores its warning state rather than
   * silently starting the count over.
   */
  consecutive_fallbacks: number;
  /** Server wall-clock ms. */
  server_timestamp_ms: number;
  /** Server **monotonic** ms of the pending unified start (ADR-0003 §5). */
  start_at_server_ms?: number | null;
}

/**
 * Server -> other roles, broadcast when a stale single-connection role
 * (`student`/`wizard`) was taken over by a new connection (ADR-0001 §9).
 */
export interface RoleConnectionReplacedMessage {
  type: "role_connection_replaced";
  role: Role;
  server_timestamp_ms: number;
}

// --- Time sync (ADR-0003) --------------------------------------------------

/**
 * Client -> server offset/RTT probe.
 *
 * Note the mixed clock domains: `t0` is on the *client's* clock while the
 * server's `s1`/`s2` are server monotonic ms — which is exactly why the
 * offset formula below exists rather than a plain subtraction.
 */
export interface ClockSyncProbeMessage {
  type: "clock_sync_probe";
  probe_id: string;
  /** Client send time (client clock, ms). */
  t0: number;
  /**
   * Client monotonic ms at send time. Carried for client-side bookkeeping;
   * the server does not read it.
   */
  client_monotonic_ms: number;
}

/**
 * Server -> client probe response. `probe_id`/`t0` are echoed back
 * unvalidated, so they are `null` when the probe omitted them.
 *
 * With the client receive time `t3`, the client computes:
 *   RTT    = (t3 - t0) - (s2 - s1)
 *   offset = ((s1 - t0) + (s2 - t3)) / 2
 * Initial sync takes 9 samples and keeps the 5 lowest-RTT ones; continuous
 * drift correction takes 5 and keeps the lowest 3. Median, not mean
 * (ADR-0003 §4, §6).
 */
export interface ClockSyncResponseMessage {
  type: "clock_sync_response";
  probe_id: string | null;
  t0: number | null;
  /** Server receive time (server monotonic ms). */
  s1: number;
  /** Server send time (server monotonic ms). */
  s2: number;
}

/**
 * Client -> server final, already-filtered offset estimate (ADR-0003 §6).
 * Best-effort: the server silently drops malformed reports and never replies.
 * Each valid report increments that connection's `clock_offset_version`.
 */
export interface ClockOffsetReportMessage {
  type: "clock_offset_report";
  offset_ms: number;
  sample_count: number;
  rtt_ms: number;
}

/**
 * Server -> all roles, broadcast on `session_started` (ADR-0003 §5).
 * `start_at_server_ms` is fixed at server monotonic time + 2000ms so every
 * client (and the recorder) enters `active` together.
 */
export interface SessionStartScheduledMessage {
  type: "session_start_scheduled";
  session_id: string;
  start_at_server_ms: number;
  server_timestamp_ms: number;
}

// --- Fallback (ADR-0002 §2, §5) --------------------------------------------

/**
 * Payload of {@link RequestFallbackMessage}. A short-lived command like any
 * other (ADR-0001 §5): the server runs the same expiry / snapshot-revision
 * validation before resolving a clip, and `command_id` doubles as the ACK
 * de-duplication key.
 */
export interface RequestFallbackPayload extends ShortLivedCommandPayload {
  fallback_category: FallbackCategory;
  /**
   * Optional Wizard-supplied reason (ADR-0002 §7). An open picklist — the
   * server records unknown strings rather than rejecting the fallback.
   */
  reason?: FallbackReasonValue;
}

/**
 * Wizard -> server fallback request (ADR-0002 §1, §2).
 *
 * A *control message* rather than a client-authored `fallback_used` envelope
 * because of the hybrid split: the Wizard names a semantic category and the
 * server decides which approved clip expresses it, so the client cannot know
 * the answer it is asking for. The server replies with an `event_ack` and
 * broadcasts the resulting `fallback_used` envelope (`FallbackUsedPayload`
 * in ./events) to every role.
 *
 * ADR-0002 §2 spells the discriminant `command_type` while the rest of the
 * control plane uses `type`; the server accepts either
 * (`raw.get("type") or raw.get("command_type")`). Prefer `type` when sending,
 * but consumers must handle either valid wire spelling after narrowing.
 */
export interface BaseRequestFallbackMessage {
  sequence: number;
  client_timestamp_ms: number;
  payload: RequestFallbackPayload;
}

/** Request fallback frame using the standard control-plane `type` key. */
export interface RequestFallbackTypeMessage extends BaseRequestFallbackMessage {
  type: "request_fallback";
  /** ADR-0002 §2's alternate spelling of `type`; the server accepts both. */
  command_type?: "request_fallback";
}

/** Request fallback frame using ADR-0002 §2's `command_type` key. */
export interface RequestFallbackCommandTypeMessage extends BaseRequestFallbackMessage {
  command_type: "request_fallback";
  /** Present only when a sender includes both accepted spellings. */
  type?: "request_fallback";
}

export type RequestFallbackMessage =
  | RequestFallbackTypeMessage
  | RequestFallbackCommandTypeMessage;

export interface FallbackThresholdReachedPayload {
  /** The streak that tripped the threshold (3 per ADR-0002 §5). */
  consecutive_fallbacks: number;
}

/**
 * Server -> all roles once three fallbacks run back to back (ADR-0002 §5):
 * the system recommends the host step in rather than letting fallbacks paper
 * over a stuck interview. Advisory only — the session is not paused.
 *
 * ADR-0002 §5 sketches this with an `event_type` key, but the server sends it
 * as a control message: there is no `EventType` member for it and the event
 * envelope contract is locked.
 */
export interface FallbackThresholdReachedMessage {
  type: "fallback_threshold_reached";
  session_id: string;
  payload: FallbackThresholdReachedPayload;
  /** Server wall-clock ms. */
  server_timestamp_ms: number;
}

// --- Unions & guards -------------------------------------------------------

/** Control messages a client sends to the server. */
export type ClientControlMessage =
  | PingMessage
  | ReconnectHelloMessage
  | ClockSyncProbeMessage
  | ClockOffsetReportMessage
  | RequestFallbackMessage;

/** Control messages the server sends to a client. */
export type ServerControlMessage =
  | PongMessage
  | EventAckMessage
  | SessionSnapshotMessage
  | RoleConnectionReplacedMessage
  | ClockSyncResponseMessage
  | SessionStartScheduledMessage
  | FallbackThresholdReachedMessage;

export type WebSocketControlMessage = ClientControlMessage | ServerControlMessage;

/** Anything that can travel over the session WebSocket. */
export type WebSocketMessage = WebSocketControlMessage | AnyEventEnvelope;

export const CONTROL_MESSAGE_TYPES = [
  "ping",
  "pong",
  "event_ack",
  "reconnect_hello",
  "session_snapshot",
  "role_connection_replaced",
  "clock_sync_probe",
  "clock_sync_response",
  "clock_offset_report",
  "session_start_scheduled",
  "request_fallback",
  "fallback_threshold_reached",
] as const;

const EVENT_ACK_STATUSES = ["accepted", "duplicate", "rejected"] as const;
const SESSION_STATES = ["created", "starting", "active", "paused", "ended"] as const;
const PLAYBACK_STATES = ["idle", "playing"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasString(record: Record<string, unknown>, key: string): boolean {
  return typeof record[key] === "string";
}

function hasNumber(record: Record<string, unknown>, key: string): boolean {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value);
}

function hasStringArray(record: Record<string, unknown>, key: string): boolean {
  return Array.isArray(record[key]) && record[key].every((item) => typeof item === "string");
}

function isStringOrNull(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isNumberOrNull(value: unknown): value is number | null {
  return (typeof value === "number" && Number.isFinite(value)) || value === null;
}

function isKnownValue<T extends readonly string[]>(
  values: T,
  value: unknown,
): value is T[number] {
  return typeof value === "string" && (values as readonly string[]).includes(value);
}

function hasOptionalStringOrNull(record: Record<string, unknown>, key: string): boolean {
  return !(key in record) || isStringOrNull(record[key]);
}

function hasOptionalNumberOrNull(record: Record<string, unknown>, key: string): boolean {
  return !(key in record) || isNumberOrNull(record[key]);
}

/**
 * The control-message discriminant, mirroring the server's own resolution
 * order (`raw.get("type") or raw.get("command_type")` in `_handle_message`).
 *
 * ADR-0002 §2 writes `request_fallback` with a `command_type` key while the
 * rest of the control plane uses `type`, so a frame spelled either way has to
 * be recognised here. Note the `or`, not a null-coalesce: an empty `type`
 * falls through to `command_type` on the server too.
 */
export function controlMessageType(message: Record<string, unknown>): unknown {
  const type = message.type;
  if (typeof type === "string" && type.length > 0) {
    return type;
  }
  return message.command_type;
}

/**
 * True when `message` is a control message rather than an event envelope.
 *
 * Discriminates on a known top-level `type` (or ADR-0002 §2's `command_type`
 * spelling) plus the minimum required fields for that control message.
 * Messages carrying `event_type` are not control messages, matching the
 * server's own dispatch order.
 */
export function isWebSocketControlMessage(
  message: unknown,
): message is WebSocketControlMessage {
  if (!isRecord(message) || "event_type" in message) {
    return false;
  }

  switch (controlMessageType(message)) {
    case "ping":
      return hasString(message, "ping_id") && hasNumber(message, "client_timestamp_ms");
    case "pong":
      return isStringOrNull(message.ping_id) && hasNumber(message, "server_timestamp_ms");
    case "event_ack":
      return (
        isStringOrNull(message.event_id) &&
        isKnownValue(EVENT_ACK_STATUSES, message.status) &&
        hasOptionalStringOrNull(message, "reason")
      );
    case "reconnect_hello":
      return (
        hasString(message, "session_id") &&
        isKnownValue(ROLE_VALUES, message.role) &&
        hasNumber(message, "last_snapshot_revision") &&
        hasNumber(message, "last_acked_sequence") &&
        hasStringArray(message, "pending_event_ids")
      );
    case "session_snapshot":
      return (
        hasNumber(message, "snapshot_revision") &&
        isKnownValue(SESSION_STATES, message.session_state) &&
        isKnownValue(PLAYBACK_STATES, message.playback_state) &&
        isStringOrNull(message.active_clip) &&
        hasString(message, "character_state") &&
        hasNumber(message, "cooperation_level") &&
        hasStringArray(message, "revealed_facts") &&
        hasNumber(message, "consecutive_fallbacks") &&
        hasNumber(message, "server_timestamp_ms") &&
        hasOptionalNumberOrNull(message, "start_at_server_ms")
      );
    case "role_connection_replaced":
      return (
        isKnownValue(ROLE_VALUES, message.role) &&
        hasNumber(message, "server_timestamp_ms")
      );
    case "clock_sync_probe":
      return (
        hasString(message, "probe_id") &&
        hasNumber(message, "t0") &&
        hasNumber(message, "client_monotonic_ms")
      );
    case "clock_sync_response":
      return (
        isStringOrNull(message.probe_id) &&
        isNumberOrNull(message.t0) &&
        hasNumber(message, "s1") &&
        hasNumber(message, "s2")
      );
    case "clock_offset_report":
      return (
        hasNumber(message, "offset_ms") &&
        hasNumber(message, "sample_count") &&
        hasNumber(message, "rtt_ms")
      );
    case "session_start_scheduled":
      return (
        hasString(message, "session_id") &&
        hasNumber(message, "start_at_server_ms") &&
        hasNumber(message, "server_timestamp_ms")
      );
    case "request_fallback": {
      // The same fields the server checks before it will resolve a clip: the
      // short-lived command triple (ADR-0001 §5) plus a known category.
      const payload = message.payload;
      return (
        hasNumber(message, "sequence") &&
        hasNumber(message, "client_timestamp_ms") &&
        isRecord(payload) &&
        hasString(payload, "command_id") &&
        hasNumber(payload, "expires_at_ms") &&
        hasNumber(payload, "snapshot_revision") &&
        isKnownValue(FALLBACK_CATEGORIES, payload.fallback_category)
      );
    }
    case "fallback_threshold_reached": {
      const payload = message.payload;
      return (
        hasString(message, "session_id") &&
        hasNumber(message, "server_timestamp_ms") &&
        isRecord(payload) &&
        hasNumber(payload, "consecutive_fallbacks")
      );
    }
    default:
      return false;
  }
}

/**
 * True when `message` is a standard event envelope (spec §9). The payload is
 * left unnarrowed; callers switch on `event_type` to refine it further.
 */
export function isEventEnvelope(message: unknown): message is AnyEventEnvelope {
  return (
    isRecord(message) &&
    isKnownValue(EVENT_TYPES, message.event_type) &&
    hasString(message, "session_id") &&
    hasNumber(message, "sequence") &&
    hasNumber(message, "client_timestamp_ms") &&
    hasOptionalNumberOrNull(message, "server_timestamp_ms") &&
    hasOptionalNumberOrNull(message, "clock_offset_ms") &&
    isKnownValue(SOURCE_VALUES, message.source) &&
    isRecord(message.payload)
  );
}
