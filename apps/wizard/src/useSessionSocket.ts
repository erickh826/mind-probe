// The Wizard console's WebSocket client (ADR-0001, ADR-0002, ADR-0003).
//
// One hook owns the whole client half of the realtime protocol:
//
//   * heartbeat        — 10s ping, 5s pong deadline, 3 misses => offline (§2)
//   * reconnect        — exponential backoff, then snapshot-first
//                        `reconnect_hello` before anything is resent (§4)
//   * clock sync       — probe/response sampling so `expires_at_ms` can be
//                        expressed on the server's monotonic clock (ADR-0003)
//   * short-lived cmds — `command_id` / `expires_at_ms` / `snapshot_revision`
//                        attached automatically (§5)
//   * ACK bookkeeping  — `last_acked_sequence` and the pending set (§7)
//
// Everything on the wire is typed against `@mind-probe/shared`; this file adds
// no message shapes of its own.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isEventEnvelope, isWebSocketControlMessage } from "@mind-probe/shared";
import type {
  AnyEventEnvelope,
  ClipCommandPayload,
  ClipInterruptedPayload,
  EventAckMessage,
  EventType,
  FallbackCategory,
  FallbackReasonValue,
  PauseReason,
  RequestFallbackTypeMessage,
  ReturnToIdlePayload,
  SessionPausedPayload,
  SessionSnapshotMessage,
  ShortLivedCommandPayload,
  UncoveredQuestionPayload,
} from "@mind-probe/shared";
import { WIZARD_ROLE, sessionSocketUrl } from "./config";
import { INITIAL_SESSION_VIEW } from "./types";
import type {
  ConnectionStatus,
  ConsoleLogEntry,
  ConsoleLogLevel,
  SessionView,
} from "./types";

// --- Protocol constants (ADR-0001 §2, §5; ADR-0003 §4/§6) ------------------

/** Heartbeat interval. Mirrors `PING_INTERVAL_MS` in server/app/websocket.py. */
const PING_INTERVAL_MS = 10_000;
/** How long a `pong` may take before the ping counts as missed. */
const PONG_WAIT_MS = 5_000;
/** Consecutive missed pongs that mark the connection offline. */
const MAX_MISSED_HEARTBEATS = 3;
/** Reconnect backoff ladder, capped (ADR-0001 §3). */
const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
/** Time-to-live stamped onto every short-lived command (ADR-0001 §5). */
const COMMAND_TTL_MS = 5_000;
/** Initial clock-sync burst: 9 probes, keep the 5 lowest-RTT (ADR-0003 §4). */
const CLOCK_SYNC_SAMPLES = 9;
const CLOCK_SYNC_KEEP_LOWEST = 5;
const CLOCK_SYNC_SPACING_MS = 120;
/** Coalescing window for authoritative snapshot re-reads. */
const SNAPSHOT_RESYNC_DEBOUNCE_MS = 150;
/** Rolling activity log size. */
const MAX_LOG_ENTRIES = 120;
/** Fallback streak at which ADR-0002 §5 asks the host to step in. */
export const FALLBACK_THRESHOLD = 3;

/**
 * Close codes the server uses for terminal refusals (4400 invalid_role, 4401
 * auth, 4404 unknown session, 4409 role conflict / taken over). Reconnecting
 * cannot fix any of them, so the ladder stops and the Wizard is told why.
 */
const TERMINAL_CLOSE_CODES = new Set([4400, 4401, 4404, 4409]);

/**
 * Event types for which the server bumps `snapshot_revision`. Mirrors
 * `_apply_snapshot_update` in server/app/websocket.py — see
 * `bumpLocalRevision` below for why the client tracks this at all.
 */
const REVISION_BUMPING_EVENTS = new Set<EventType>([
  "session_started",
  "session_paused",
  "session_resumed",
  "session_ended",
  "clip_command",
  "clip_started",
  "clip_ended",
  "clip_interrupted",
  "return_to_idle",
  "fallback_used",
]);

// --- Helpers ---------------------------------------------------------------

function newId(prefix: string): string {
  const unique =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `${prefix}-${unique}`;
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** One RTT/offset sample from a `clock_sync_probe` round trip (ADR-0003 §3). */
interface ClockSample {
  rtt_ms: number;
  offset_ms: number;
}

export interface ClockState {
  /** Client clock -> server monotonic ms. `null` until the first burst lands. */
  offset_ms: number | null;
  rtt_ms: number | null;
  sample_count: number;
}

export interface AckView {
  event_id: string | null;
  status: EventAckMessage["status"];
  reason: string | null;
}

/** A frame the console has sent but not yet seen an `event_ack` for. */
interface PendingFrame {
  dedup_key: string;
  event_type: EventType | "request_fallback";
  /** Envelope sequence, so an ACK can advance `last_acked_sequence` (§7). */
  sequence: number;
  /** Kept verbatim so a reconnect can resend it unchanged. */
  frame: unknown;
  /** Short-lived commands are dropped rather than replayed (ADR-0001 §5). */
  short_lived: boolean;
}

export interface SessionSocket {
  status: ConnectionStatus;
  /** Terminal close reason, e.g. "role_already_connected". */
  closedReason: string | null;
  session: SessionView;
  clock: ClockState;
  lastAck: AckView | null;
  pendingCount: number;
  /** Streak that tripped ADR-0002 §5, or `null` once dismissed. */
  thresholdAlert: number | null;
  dismissThresholdAlert: () => void;
  log: ConsoleLogEntry[];
  /** Wall-clock ms at which the active clip began, for played_duration_ms. */
  activeClipStartedAtMs: number | null;
  sendClipCommand: (clipId: string) => void;
  sendReturnToIdle: () => void;
  sendClipInterrupted: (reason: string) => void;
  sendSessionPaused: (reason: PauseReason) => void;
  sendSessionResumed: () => void;
  sendRequestFallback: (
    category: FallbackCategory,
    reason?: FallbackReasonValue,
  ) => void;
  sendUncoveredQuestion: (payload: UncoveredQuestionPayload) => void;
}

/**
 * Connect to `/ws/{sessionId}/wizard` and expose the console's view of the
 * session. Pass `null` to stay disconnected (before the Wizard has joined).
 *
 * The socket is driven from refs rather than state on purpose: a re-render
 * caused by, say, an incoming log line must never tear down and rebuild a live
 * connection mid-interrogation.
 */
export function useSessionSocket(sessionId: string | null): SessionSocket {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [closedReason, setClosedReason] = useState<string | null>(null);
  const [session, setSession] = useState<SessionView>(INITIAL_SESSION_VIEW);
  const [clock, setClock] = useState<ClockState>({
    offset_ms: null,
    rtt_ms: null,
    sample_count: 0,
  });
  const [lastAck, setLastAck] = useState<AckView | null>(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [thresholdAlert, setThresholdAlert] = useState<number | null>(null);
  const [log, setLog] = useState<ConsoleLogEntry[]>([]);
  const [activeClipStartedAtMs, setActiveClipStartedAtMs] = useState<number | null>(
    null,
  );

  const socketRef = useRef<WebSocket | null>(null);
  const shouldReconnectRef = useRef(false);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const hasConnectedOnceRef = useRef(false);

  const pingTimerRef = useRef<number | null>(null);
  const pongTimersRef = useRef(new Map<string, number>());
  const missedPongsRef = useRef(0);

  const clockSamplesRef = useRef<ClockSample[]>([]);
  const probeSentAtRef = useRef(new Map<string, number>());
  const clockOffsetRef = useRef<number | null>(null);
  const clockTimersRef = useRef<number[]>([]);

  // Survives socket teardown on purpose: the server's fallback de-duplication
  // key is `session_id:source:sequence`, so a counter that restarted at 0 after
  // a reconnect would collide with keys it had already used (ADR-0001 §7).
  const sequenceRef = useRef(0);
  const lastAckedSequenceRef = useRef(-1);
  const pendingRef = useRef(new Map<string, PendingFrame>());
  const revisionRef = useRef(0);
  const resyncTimerRef = useRef<number | null>(null);
  /** Set on every socket open: the next snapshot triggers the §4 replay. */
  const replayAfterSnapshotRef = useRef(false);
  const activeClipRef = useRef<string | null>(null);
  const clipStartedAtRef = useRef<number | null>(null);

  const appendLog = useCallback((level: ConsoleLogLevel, text: string) => {
    setLog((entries) => {
      const entry: ConsoleLogEntry = {
        id: newId("log"),
        at_ms: Date.now(),
        level,
        text,
      };
      return [entry, ...entries].slice(0, MAX_LOG_ENTRIES);
    });
  }, []);

  const setActiveClip = useCallback((clipId: string | null, startedAtMs: number | null) => {
    activeClipRef.current = clipId;
    clipStartedAtRef.current = startedAtMs;
    setActiveClipStartedAtMs(startedAtMs);
  }, []);

  // --- Low-level send ------------------------------------------------------

  const rawSend = useCallback((frame: unknown): boolean => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    socket.send(JSON.stringify(frame));
    return true;
  }, []);

  /**
   * Current time on the *server's monotonic* clock (ADR-0003 §1).
   *
   * This matters for more than tidiness: `_validate_short_lived_command` in
   * server/app/websocket.py compares `expires_at_ms` against
   * `time.monotonic_ns()`, which counts from boot, so a raw `Date.now()`
   * deadline sits ~1.7e12 ms in its future and the TTL never fires. Before the
   * first clock-sync burst completes there is no offset to apply and the
   * uncorrected value is used — permissive rather than rejecting, which is the
   * right failure direction mid-interrogation.
   */
  const serverMonotonicNow = useCallback((): number => {
    const offset = clockOffsetRef.current;
    return offset === null ? Date.now() : Date.now() + offset;
  }, []);

  /**
   * Ask the server for the authoritative snapshot.
   *
   * `reconnect_hello` is the protocol's only snapshot fetch, and on the server
   * it is side-effect free beyond a heartbeat touch. It is needed on more than
   * reconnects because `snapshot_revision` can advance by more than one per
   * accepted event (`_adopt_db_revision` reconciles the in-memory and persisted
   * counters by maximum), so a purely local mirror would drift low and start
   * earning `stale_snapshot_revision` rejections.
   */
  const requestSnapshot = useCallback(() => {
    if (sessionId === null) {
      return;
    }
    // Short-lived commands are never replayed: by the time a reconnect
    // completes their deadline has passed, and ADR-0001 §5 says a stale command
    // must be dropped rather than resurrected against a newer snapshot.
    for (const [key, pending] of pendingRef.current) {
      if (pending.short_lived) {
        pendingRef.current.delete(key);
      }
    }
    setPendingCount(pendingRef.current.size);
    rawSend({
      type: "reconnect_hello",
      session_id: sessionId,
      role: WIZARD_ROLE,
      last_snapshot_revision: revisionRef.current,
      last_acked_sequence: lastAckedSequenceRef.current,
      pending_event_ids: [...pendingRef.current.keys()],
    });
  }, [rawSend, sessionId]);

  const scheduleSnapshotResync = useCallback(() => {
    if (resyncTimerRef.current !== null) {
      return;
    }
    resyncTimerRef.current = window.setTimeout(() => {
      resyncTimerRef.current = null;
      requestSnapshot();
    }, SNAPSHOT_RESYNC_DEBOUNCE_MS);
  }, [requestSnapshot]);

  /**
   * Optimistically mirror the server's `snapshot_revision += 1`.
   *
   * Only ever called for an **accepted** event. Bumping at send time would
   * ratchet the counter past the server on every rejection, and since the
   * server rejects on `snapshot_revision < snapshot.snapshot_revision`, an
   * inflated local number makes that staleness check pass by construction —
   * disabling the very guard of ADR-0001 §5. The bump covers the round trip
   * until `scheduleSnapshotResync` brings back the authoritative value.
   */
  const bumpLocalRevision = useCallback((eventType: EventType | "request_fallback") => {
    if (eventType === "request_fallback" || !REVISION_BUMPING_EVENTS.has(eventType)) {
      // The fallback's revision bump rides in on the `fallback_used` envelope,
      // which the server broadcasts back to the requesting Wizard too.
      return;
    }
    revisionRef.current += 1;
    setSession((view) => ({ ...view, snapshot_revision: revisionRef.current }));
  }, []);

  // --- Outbound events -----------------------------------------------------

  const trackPending = useCallback((pending: PendingFrame) => {
    pendingRef.current.set(pending.dedup_key, pending);
    setPendingCount(pendingRef.current.size);
  }, []);

  /** Build the `command_id`/`expires_at_ms`/`snapshot_revision` triple. */
  const shortLivedPayload = useCallback((): ShortLivedCommandPayload => {
    return {
      command_id: newId("cmd"),
      expires_at_ms: Math.round(serverMonotonicNow() + COMMAND_TTL_MS),
      snapshot_revision: revisionRef.current,
    };
  }, [serverMonotonicNow]);

  /** Send one spec §9 envelope, tracking it for ACK de-duplication. */
  const sendEnvelope = useCallback(
    (
      eventType: EventType,
      payload: Record<string, unknown>,
      shortLived: boolean,
    ) => {
      if (sessionId === null) {
        return;
      }
      const sequence = sequenceRef.current;
      const envelope: AnyEventEnvelope = {
        session_id: sessionId,
        sequence,
        client_timestamp_ms: Date.now(),
        source: "wizard",
        event_type: eventType,
        payload,
      };
      const dedupKey =
        typeof payload.event_id === "string"
          ? payload.event_id
          : typeof payload.command_id === "string"
            ? payload.command_id
            : `${sessionId}:wizard:${sequence}`;

      if (!rawSend(envelope)) {
        appendLog("error", `連線中斷，${eventType} 未送出 (not sent: offline)`);
        return;
      }
      sequenceRef.current = sequence + 1;
      trackPending({
        dedup_key: dedupKey,
        event_type: eventType,
        sequence,
        frame: envelope,
        short_lived: shortLived,
      });
      appendLog("info", `→ ${eventType}`);
    },
    [appendLog, rawSend, sessionId, trackPending],
  );

  const sendClipCommand = useCallback(
    (clipId: string) => {
      const payload: ClipCommandPayload = {
        ...shortLivedPayload(),
        clip_id: clipId,
      };
      sendEnvelope("clip_command", { ...payload }, true);
      // The server does not echo our own events back, so start the playback
      // stopwatch here; `clip_started`/`fallback_used` reset it if one arrives.
      setActiveClip(clipId, Date.now());
    },
    [sendEnvelope, setActiveClip, shortLivedPayload],
  );

  const sendReturnToIdle = useCallback(() => {
    const payload: ReturnToIdlePayload = shortLivedPayload();
    sendEnvelope("return_to_idle", { ...payload }, true);
    setActiveClip(null, null);
  }, [sendEnvelope, setActiveClip, shortLivedPayload]);

  /**
   * `B` hotkey: the student talked over the suspect (spec §7.2).
   *
   * `played_duration_ms` is measured from when playback actually began rather
   * than guessed — the teacher timeline uses it to reconstruct how much of the
   * answer the student heard before interrupting.
   */
  const sendClipInterrupted = useCallback(
    (reason: string) => {
      const clipId = activeClipRef.current;
      if (clipId === null) {
        appendLog("warn", "沒有播放中的片段，忽略打斷 (no active clip)");
        return;
      }
      const startedAt = clipStartedAtRef.current;
      const payload: ClipInterruptedPayload = {
        event_id: newId("evt"),
        clip_id: clipId,
        played_duration_ms:
          startedAt === null ? 0 : Math.max(0, Date.now() - startedAt),
        reason,
      };
      sendEnvelope("clip_interrupted", { ...payload }, false);
      setActiveClip(null, null);
    },
    [appendLog, sendEnvelope, setActiveClip],
  );

  const sendSessionPaused = useCallback(
    (reason: PauseReason) => {
      const payload: SessionPausedPayload = { ...shortLivedPayload(), reason };
      sendEnvelope("session_paused", { ...payload }, true);
    },
    [sendEnvelope, shortLivedPayload],
  );

  /**
   * Resume is not in `SHORT_LIVED_COMMAND_TYPES`, so it carries only an
   * `event_id` for de-duplication — stamping it with a deadline would let a
   * slow network silently swallow the one command that ends a pause.
   */
  const sendSessionResumed = useCallback(() => {
    sendEnvelope("session_resumed", { event_id: newId("evt") }, false);
  }, [sendEnvelope]);

  /**
   * ADR-0002 §1/§2: the Wizard names a semantic *category* and the server
   * decides which approved clip expresses it, so this is a control message and
   * not a client-authored `fallback_used` envelope (which the server rejects
   * with `fallback_is_server_resolved`).
   */
  const sendRequestFallback = useCallback(
    (category: FallbackCategory, reason?: FallbackReasonValue) => {
      const payload = {
        ...shortLivedPayload(),
        fallback_category: category,
        ...(reason ? { reason } : {}),
      };
      const sequence = sequenceRef.current;
      const frame: RequestFallbackTypeMessage = {
        type: "request_fallback",
        sequence,
        client_timestamp_ms: Date.now(),
        payload,
      };
      if (!rawSend(frame)) {
        appendLog("error", "連線中斷，fallback 未送出 (not sent: offline)");
        return;
      }
      sequenceRef.current = sequence + 1;
      trackPending({
        dedup_key: payload.command_id,
        event_type: "request_fallback",
        sequence,
        frame,
        short_lived: true,
      });
      // The resulting `fallback_used` is broadcast to *every* role including
      // this one, so the revision bump and clip id arrive with that envelope.
      appendLog("info", `→ request_fallback ${category}`);
    },
    [appendLog, rawSend, shortLivedPayload, trackPending],
  );

  /** `M` hotkey (spec §8, ADR-0002 §7). Broadcast to teacher/observer only. */
  const sendUncoveredQuestion = useCallback(
    (payload: UncoveredQuestionPayload) => {
      sendEnvelope("uncovered_question", { event_id: newId("evt"), ...payload }, false);
    },
    [sendEnvelope],
  );

  // --- Inbound handling ----------------------------------------------------

  const applySnapshot = useCallback(
    (message: SessionSnapshotMessage) => {
      // Assign, never max(): the snapshot is authoritative, and snapshots
      // arrive in order on a single socket. Keeping a higher local number
      // would let a stale command sail past the server's staleness check.
      revisionRef.current = message.snapshot_revision;
      setSession({
        snapshot_revision: message.snapshot_revision,
        session_state: message.session_state,
        playback_state: message.playback_state,
        active_clip: message.active_clip,
        character_state: message.character_state,
        cooperation_level: message.cooperation_level,
        revealed_facts: message.revealed_facts,
        consecutive_fallbacks: message.consecutive_fallbacks,
        start_at_server_ms: message.start_at_server_ms ?? null,
      });
      if (message.playback_state === "playing") {
        setActiveClip(message.active_clip, clipStartedAtRef.current ?? Date.now());
      } else {
        setActiveClip(null, null);
      }
      if (message.consecutive_fallbacks < FALLBACK_THRESHOLD) {
        setThresholdAlert(null);
      }

      // ADR-0001 §4: the client applies the authoritative snapshot *before*
      // resending anything from `pending_event_ids`. The resent frames keep
      // their original `event_id`, so the server answers a copy it already
      // stored with `duplicate` rather than recording it twice.
      if (replayAfterSnapshotRef.current) {
        replayAfterSnapshotRef.current = false;
        for (const pending of pendingRef.current.values()) {
          rawSend(pending.frame);
        }
        if (pendingRef.current.size > 0) {
          appendLog("info", `重送 ${pendingRef.current.size} 筆未 ACK 事件 (replayed)`);
        }
      }
    },
    [appendLog, rawSend, setActiveClip],
  );

  /**
   * Mirror `_apply_snapshot_update` for events broadcast by other roles.
   *
   * The server only pushes a full `session_snapshot` on request, so without
   * this the console would show a frozen playback state between resyncs.
   */
  const applyBroadcastEnvelope = useCallback(
    (envelope: AnyEventEnvelope) => {
      const payload = envelope.payload as Record<string, unknown>;
      setSession((view) => {
        const next = { ...view };
        switch (envelope.event_type) {
          case "session_started":
            next.session_state = "starting";
            break;
          case "session_paused":
            next.session_state = "paused";
            break;
          case "session_resumed":
            next.session_state = "active";
            next.start_at_server_ms = null;
            break;
          case "session_ended":
            next.session_state = "ended";
            break;
          case "fallback_used":
            next.playback_state = "playing";
            if (typeof payload.resolved_clip_id === "string") {
              next.active_clip = payload.resolved_clip_id;
            }
            if (typeof payload.consecutive_fallbacks === "number") {
              next.consecutive_fallbacks = payload.consecutive_fallbacks;
            }
            break;
          case "clip_command":
          case "clip_started":
            next.playback_state = "playing";
            if (typeof payload.clip_id === "string") {
              next.active_clip = payload.clip_id;
            }
            break;
          case "clip_ended":
          case "clip_interrupted":
          case "return_to_idle":
            next.playback_state = "idle";
            next.active_clip = null;
            break;
          default:
            return view;
        }
        return next;
      });

      switch (envelope.event_type) {
        case "clip_command":
        case "clip_started":
          if (typeof payload.clip_id === "string") {
            setActiveClip(payload.clip_id, Date.now());
          }
          break;
        case "fallback_used":
          if (typeof payload.resolved_clip_id === "string") {
            setActiveClip(payload.resolved_clip_id, Date.now());
          }
          break;
        case "clip_ended":
        case "clip_interrupted":
        case "return_to_idle":
          setActiveClip(null, null);
          break;
        default:
          break;
      }
    },
    [setActiveClip],
  );

  const handleAck = useCallback(
    (message: EventAckMessage) => {
      const key = message.event_id;
      setLastAck({
        event_id: key,
        status: message.status,
        reason: message.reason ?? null,
      });
      const pending = key === null ? undefined : pendingRef.current.get(key);
      if (key !== null && pending !== undefined) {
        pendingRef.current.delete(key);
        setPendingCount(pendingRef.current.size);
      }
      if (pending !== undefined && message.status !== "rejected") {
        // ADR-0001 §7: the high-water mark a `reconnect_hello` reports.
        lastAckedSequenceRef.current = Math.max(
          lastAckedSequenceRef.current,
          pending.sequence,
        );
      }
      if (message.status === "rejected") {
        appendLog("warn", `✗ 伺服器拒絕 (rejected): ${message.reason ?? "unknown"}`);
        if (message.reason === "stale_snapshot_revision") {
          // Our revision was behind the server's; re-read it now so the Wizard
          // can simply press the key again.
          requestSnapshot();
        }
        return;
      }
      if (message.status === "accepted") {
        if (pending !== undefined) {
          bumpLocalRevision(pending.event_type);
        }
        scheduleSnapshotResync();
      }
    },
    [appendLog, bumpLocalRevision, requestSnapshot, scheduleSnapshotResync],
  );

  // --- Clock sync (ADR-0003 §3, §4, §6) ------------------------------------

  const startClockSync = useCallback(() => {
    clockSamplesRef.current = [];
    probeSentAtRef.current.clear();
    for (const timer of clockTimersRef.current) {
      window.clearTimeout(timer);
    }
    clockTimersRef.current = [];

    for (let i = 0; i < CLOCK_SYNC_SAMPLES; i += 1) {
      const timer = window.setTimeout(() => {
        const probeId = newId("probe");
        const t0 = Date.now();
        probeSentAtRef.current.set(probeId, t0);
        rawSend({
          type: "clock_sync_probe",
          probe_id: probeId,
          t0,
          // Carried for client bookkeeping only; the server does not read it.
          client_monotonic_ms: Math.round(performance.now()),
        });
      }, i * CLOCK_SYNC_SPACING_MS);
      clockTimersRef.current.push(timer);
    }
  }, [rawSend]);

  const handleClockSyncResponse = useCallback(
    (probeId: string | null, t0Echo: number | null, s1: number, s2: number) => {
      const recorded = probeId === null ? undefined : probeSentAtRef.current.get(probeId);
      const t0 = recorded ?? t0Echo;
      if (t0 === null || t0 === undefined) {
        return;
      }
      if (probeId !== null) {
        probeSentAtRef.current.delete(probeId);
      }
      const t3 = Date.now();
      // ADR-0003 §3: RTT excludes the server's own processing gap (s2 - s1).
      const rtt = t3 - t0 - (s2 - s1);
      const offset = (s1 - t0 + (s2 - t3)) / 2;
      clockSamplesRef.current.push({ rtt_ms: rtt, offset_ms: offset });

      if (clockSamplesRef.current.length < CLOCK_SYNC_SAMPLES) {
        return;
      }
      // ADR-0003 §4/§6: keep the lowest-RTT samples, then take the *median*
      // offset — a mean lets one queued packet drag the estimate.
      const best = [...clockSamplesRef.current]
        .sort((a, b) => a.rtt_ms - b.rtt_ms)
        .slice(0, CLOCK_SYNC_KEEP_LOWEST);
      const offsetMs = Math.round(median(best.map((sample) => sample.offset_ms)));
      const rttMs = Math.round(median(best.map((sample) => sample.rtt_ms)));
      clockOffsetRef.current = offsetMs;
      setClock({ offset_ms: offsetMs, rtt_ms: rttMs, sample_count: best.length });
      rawSend({
        type: "clock_offset_report",
        offset_ms: offsetMs,
        sample_count: best.length,
        rtt_ms: rttMs,
      });
      appendLog("info", `時鐘同步完成 offset=${offsetMs}ms rtt=${rttMs}ms`);
    },
    [appendLog, rawSend],
  );

  // --- Heartbeat (ADR-0001 §2) ---------------------------------------------

  const clearHeartbeat = useCallback(() => {
    if (pingTimerRef.current !== null) {
      window.clearInterval(pingTimerRef.current);
      pingTimerRef.current = null;
    }
    for (const timer of pongTimersRef.current.values()) {
      window.clearTimeout(timer);
    }
    pongTimersRef.current.clear();
  }, []);

  const startHeartbeat = useCallback(() => {
    clearHeartbeat();
    missedPongsRef.current = 0;
    pingTimerRef.current = window.setInterval(() => {
      const pingId = newId("ping");
      if (!rawSend({ type: "ping", ping_id: pingId, client_timestamp_ms: Date.now() })) {
        return;
      }
      const timer = window.setTimeout(() => {
        pongTimersRef.current.delete(pingId);
        missedPongsRef.current += 1;
        if (missedPongsRef.current >= MAX_MISSED_HEARTBEATS) {
          appendLog("error", "心跳連續逾時，重新連線 (heartbeat lost)");
          // Closing is what starts the backoff ladder over in `onclose`.
          socketRef.current?.close();
        } else {
          setStatus("degraded");
        }
      }, PONG_WAIT_MS);
      pongTimersRef.current.set(pingId, timer);
    }, PING_INTERVAL_MS);
  }, [appendLog, clearHeartbeat, rawSend]);

  const handlePong = useCallback((pingId: string | null) => {
    if (pingId !== null) {
      const timer = pongTimersRef.current.get(pingId);
      if (timer !== undefined) {
        window.clearTimeout(timer);
        pongTimersRef.current.delete(pingId);
      }
    }
    missedPongsRef.current = 0;
    setStatus((current) => (current === "degraded" ? "open" : current));
  }, []);

  // --- Message router ------------------------------------------------------

  const handleMessage = useCallback(
    (raw: string) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        appendLog("warn", "收到無法解析的訊息 (unparsable frame)");
        return;
      }

      // Control messages carry a top-level `type`, envelopes carry
      // `event_type` — the same dispatch order the server itself uses.
      if (isWebSocketControlMessage(parsed)) {
        switch (parsed.type) {
          case "pong":
            handlePong(parsed.ping_id);
            return;
          case "event_ack":
            handleAck(parsed);
            return;
          case "session_snapshot":
            applySnapshot(parsed);
            return;
          case "clock_sync_response":
            handleClockSyncResponse(parsed.probe_id, parsed.t0, parsed.s1, parsed.s2);
            return;
          case "session_start_scheduled": {
            const startAt = parsed.start_at_server_ms;
            setSession((view) => ({
              ...view,
              session_state: "starting",
              start_at_server_ms: startAt,
            }));
            appendLog("info", "Session 已排定統一開始時間 (start scheduled)");
            return;
          }
          case "role_connection_replaced":
            appendLog("warn", `${parsed.role} 連線已被接管 (connection replaced)`);
            return;
          case "fallback_threshold_reached": {
            // ADR-0002 §5: advisory only — the session is not paused.
            const streak = parsed.payload.consecutive_fallbacks;
            setThresholdAlert(streak);
            setSession((view) => ({ ...view, consecutive_fallbacks: streak }));
            appendLog("warn", `連續 ${streak} 次 fallback，建議主持人介入`);
            return;
          }
          default:
            return;
        }
      }

      if (isEventEnvelope(parsed)) {
        applyBroadcastEnvelope(parsed);
        if (REVISION_BUMPING_EVENTS.has(parsed.event_type)) {
          scheduleSnapshotResync();
        }
        if (parsed.event_type === "fallback_used") {
          const clipId = (parsed.payload as Record<string, unknown>).resolved_clip_id;
          appendLog("info", `← fallback_used → ${String(clipId)}`);
        } else {
          appendLog("info", `← ${parsed.event_type}`);
        }
      }
    },
    [
      appendLog,
      applyBroadcastEnvelope,
      applySnapshot,
      handleAck,
      handleClockSyncResponse,
      handlePong,
      scheduleSnapshotResync,
    ],
  );

  // --- Connection lifecycle (ADR-0001 §3, §4) ------------------------------

  useEffect(() => {
    if (sessionId === null) {
      setStatus("idle");
      return;
    }

    shouldReconnectRef.current = true;
    reconnectAttemptRef.current = 0;
    hasConnectedOnceRef.current = false;
    let disposed = false;

    const connect = () => {
      if (disposed || !shouldReconnectRef.current) {
        return;
      }
      setStatus(hasConnectedOnceRef.current ? "reconnecting" : "connecting");
      const socket = new WebSocket(sessionSocketUrl(sessionId));
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) {
          socket.close();
          return;
        }
        hasConnectedOnceRef.current = true;
        reconnectAttemptRef.current = 0;
        setStatus("open");
        setClosedReason(null);
        appendLog("info", "WebSocket 已連線 (connected)");
        // ADR-0001 §4: snapshot first, and only then the replay of whatever
        // was still unacknowledged when the old socket died.
        replayAfterSnapshotRef.current = true;
        requestSnapshot();
        startClockSync();
        startHeartbeat();
      };

      socket.onmessage = (event) => {
        if (typeof event.data === "string") {
          handleMessage(event.data);
        }
      };

      socket.onerror = () => {
        // `onclose` always follows, and it owns the reconnect decision.
        appendLog("warn", "WebSocket 錯誤 (socket error)");
      };

      socket.onclose = (event) => {
        clearHeartbeat();
        socketRef.current = null;
        if (disposed) {
          return;
        }
        if (TERMINAL_CLOSE_CODES.has(event.code)) {
          // 4400/4401/4404/4409 are refusals rather than glitches — retrying
          // would hammer the server with the same rejected upgrade.
          shouldReconnectRef.current = false;
          setStatus("closed");
          setClosedReason(event.reason || String(event.code));
          appendLog("error", `連線被拒絕 (${event.code}): ${event.reason || "unknown"}`);
          return;
        }
        if (!shouldReconnectRef.current) {
          setStatus("closed");
          return;
        }
        const attempt = reconnectAttemptRef.current;
        const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
        reconnectAttemptRef.current = attempt + 1;
        setStatus("reconnecting");
        appendLog("warn", `連線中斷，${delay / 1000}s 後重試 (reconnecting)`);
        reconnectTimerRef.current = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      shouldReconnectRef.current = false;
      clearHeartbeat();
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (resyncTimerRef.current !== null) {
        window.clearTimeout(resyncTimerRef.current);
        resyncTimerRef.current = null;
      }
      for (const timer of clockTimersRef.current) {
        window.clearTimeout(timer);
      }
      clockTimersRef.current = [];
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [
    appendLog,
    clearHeartbeat,
    handleMessage,
    requestSnapshot,
    sessionId,
    startClockSync,
    startHeartbeat,
  ]);

  const dismissThresholdAlert = useCallback(() => setThresholdAlert(null), []);

  return useMemo(
    () => ({
      status,
      closedReason,
      session,
      clock,
      lastAck,
      pendingCount,
      thresholdAlert,
      dismissThresholdAlert,
      log,
      activeClipStartedAtMs,
      sendClipCommand,
      sendReturnToIdle,
      sendClipInterrupted,
      sendSessionPaused,
      sendSessionResumed,
      sendRequestFallback,
      sendUncoveredQuestion,
    }),
    [
      activeClipStartedAtMs,
      clock,
      closedReason,
      dismissThresholdAlert,
      lastAck,
      log,
      pendingCount,
      sendClipCommand,
      sendClipInterrupted,
      sendRequestFallback,
      sendReturnToIdle,
      sendSessionPaused,
      sendSessionResumed,
      sendUncoveredQuestion,
      session,
      status,
      thresholdAlert,
    ],
  );
}
