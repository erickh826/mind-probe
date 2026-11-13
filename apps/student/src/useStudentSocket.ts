import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ADMISSION_TIMEOUT_MS,
  controlMessageType,
  isEventEnvelope,
  isWebSocketControlMessage,
  readAccessToken,
  SocketLifecycle,
  websocketSubprotocols,
} from "@mind-probe/shared";
import type {
  AnyEventEnvelope,
  BaseEventPayload,
  CloseKind,
  EventAckMessage,
  EventType,
  SessionSnapshotMessage,
  SocketEffects,
} from "@mind-probe/shared";
import { sessionSocketUrl } from "./config";
import { INITIAL_SESSION_VIEW, STUDENT_ROLE } from "./types";
import type {
  ClockState,
  ConnectionStatus,
  ConsoleLogEntry,
  ConsoleLogLevel,
  SessionView,
} from "./types";

const PING_INTERVAL_MS = 10_000;
const PONG_WAIT_MS = 5_000;
const MAX_MISSED_HEARTBEATS = 3;
const CLOCK_SYNC_SAMPLES = 9;
const CLOCK_SYNC_KEEP_LOWEST = 5;
const CLOCK_SYNC_SPACING_MS = 120;
const MAX_LOG_ENTRIES = 80;

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

interface ClockSample {
  rtt_ms: number;
  offset_ms: number;
}

interface PendingFrame {
  dedup_key: string;
  event_type: EventType;
  sequence: number;
  frame: AnyEventEnvelope;
}

export interface AckView {
  event_id: string | null;
  status: EventAckMessage["status"];
  reason: string | null;
}

export interface StudentSocket {
  status: ConnectionStatus;
  /**
   * The server admitted this tab: its first snapshot arrived on the current
   * socket. An open transport alone is not enough -- refusals (4429, 1013)
   * are accepted and then closed. Recording may start only while this holds.
   */
  admitted: boolean;
  /** Why the tab is not admitted, or why it was closed for good. */
  notice: CloseKind | null;
  closedReason: string | null;
  session: SessionView;
  clock: ClockState;
  currentClipId: string | null;
  lastAck: AckView | null;
  pendingCount: number;
  log: ConsoleLogEntry[];
  sendClipStarted: (clipId: string) => void;
  sendClipEnded: (clipId: string) => void;
  sendRecordingStarted: () => void;
  sendRecordingStopped: (reason?: string) => void;
  sendRecordingFailed: (problem: {
    phase: string;
    step: string;
    errorName: string;
    message: string;
  }) => void;
  sendChunkUploaded: (chunkIndex: number, timestampMs: number) => void;
}

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

function payloadString(payload: BaseEventPayload, key: string): string | null {
  const value = (payload as Record<string, unknown>)[key];
  return typeof value === "string" ? value : null;
}

function payloadNumber(payload: BaseEventPayload, key: string): number | null {
  const value = (payload as Record<string, unknown>)[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function useStudentSocket(
  sessionId: string | null,
  accessToken: string | null,
): StudentSocket {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [closedReason, setClosedReason] = useState<string | null>(null);
  const [notice, setNotice] = useState<CloseKind | null>(null);
  const [session, setSession] = useState<SessionView>(INITIAL_SESSION_VIEW);
  const [clock, setClock] = useState<ClockState>({
    offset_ms: null,
    rtt_ms: null,
    sample_count: 0,
  });
  const [currentClipId, setCurrentClipId] = useState<string | null>(null);
  const [lastAck, setLastAck] = useState<AckView | null>(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [log, setLog] = useState<ConsoleLogEntry[]>([]);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const admissionTimerRef = useRef<number | null>(null);
  /** Set while the current socket is admitted; events are sent only then. */
  const admittedRef = useRef(false);
  /** Called with the id of the socket a `session_snapshot` arrived on. */
  const admitRef = useRef<((socketId: number) => void) | null>(null);

  const pingTimerRef = useRef<number | null>(null);
  const pongTimersRef = useRef(new Map<string, number>());
  const missedPongsRef = useRef(0);

  const clockSamplesRef = useRef<ClockSample[]>([]);
  const probeSentAtRef = useRef(new Map<string, number>());
  const clockOffsetRef = useRef<number | null>(null);
  const clockTimersRef = useRef<number[]>([]);
  const clockReportSentRef = useRef(false);
  const scheduledStartTimerRef = useRef<number | null>(null);

  const sequenceRef = useRef(0);
  const lastAckedSequenceRef = useRef(-1);
  const pendingRef = useRef(new Map<string, PendingFrame>());
  const revisionRef = useRef(0);
  const replayAfterSnapshotRef = useRef(false);

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

  const rawSend = useCallback((frame: unknown): boolean => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    socket.send(JSON.stringify(frame));
    return true;
  }, []);

  const requestSnapshot = useCallback(() => {
    if (sessionId === null) {
      return;
    }
    rawSend({
      type: "reconnect_hello",
      session_id: sessionId,
      role: STUDENT_ROLE,
      last_snapshot_revision: revisionRef.current,
      last_acked_sequence: lastAckedSequenceRef.current,
      pending_event_ids: [...pendingRef.current.keys()],
    });
  }, [rawSend, sessionId]);

  const trackPending = useCallback((pending: PendingFrame) => {
    pendingRef.current.set(pending.dedup_key, pending);
    setPendingCount(pendingRef.current.size);
  }, []);

  const sendEnvelope = useCallback(
    (eventType: EventType, payload: Record<string, unknown>) => {
      if (sessionId === null) {
        return;
      }
      const sequence = sequenceRef.current;
      const eventPayload = { event_id: newId("evt"), ...payload };
      const envelope: AnyEventEnvelope = {
        session_id: sessionId,
        sequence,
        client_timestamp_ms: Date.now(),
        source: STUDENT_ROLE,
        event_type: eventType,
        payload: eventPayload as BaseEventPayload,
      };
      const dedupKey =
        typeof eventPayload.event_id === "string"
          ? eventPayload.event_id
          : `${sessionId}:${STUDENT_ROLE}:${sequence}`;

      if (!admittedRef.current || !rawSend(envelope)) {
        appendLog("warn", `${eventType} not sent: socket is offline.`);
        return;
      }
      sequenceRef.current = sequence + 1;
      trackPending({
        dedup_key: dedupKey,
        event_type: eventType,
        sequence,
        frame: envelope,
      });
    },
    [appendLog, rawSend, sessionId, trackPending],
  );

  const sendClipStarted = useCallback(
    (clipId: string) => {
      sendEnvelope("clip_started", { clip_id: clipId });
    },
    [sendEnvelope],
  );

  const sendClipEnded = useCallback(
    (clipId: string) => {
      sendEnvelope("clip_ended", { clip_id: clipId });
      setCurrentClipId(null);
      setSession((view) => ({ ...view, playback_state: "idle", active_clip: null }));
    },
    [sendEnvelope],
  );

  const sendRecordingStarted = useCallback(() => {
    sendEnvelope("recording_started", {});
  }, [sendEnvelope]);

  const sendRecordingStopped = useCallback(
    (reason?: string) => {
      sendEnvelope("recording_stopped", reason ? { reason } : {});
    },
    [sendEnvelope],
  );

  const sendRecordingFailed = useCallback(
    (problem: { phase: string; step: string; errorName: string; message: string }) => {
      sendEnvelope("recording_failed", {
        phase: problem.phase,
        step: problem.step,
        error_name: problem.errorName,
        message: problem.message,
      });
    },
    [sendEnvelope],
  );

  const sendChunkUploaded = useCallback(
    (chunkIndex: number, timestampMs: number) => {
      sendEnvelope("chunk_uploaded", {
        chunk_index: chunkIndex,
        timestamp_ms: timestampMs,
      });
    },
    [sendEnvelope],
  );

  const scheduleLocalStart = useCallback(
    (startAtServerMs: number) => {
      if (scheduledStartTimerRef.current !== null) {
        window.clearTimeout(scheduledStartTimerRef.current);
        scheduledStartTimerRef.current = null;
      }
      const offset = clockOffsetRef.current ?? 0;
      const delay = Math.max(0, Math.round(startAtServerMs - (Date.now() + offset)));
      setSession((view) => ({
        ...view,
        session_state: "starting",
        start_at_server_ms: startAtServerMs,
      }));
      scheduledStartTimerRef.current = window.setTimeout(() => {
        setSession((view) => ({
          ...view,
          session_state: "active",
          start_at_server_ms: null,
        }));
        scheduledStartTimerRef.current = null;
      }, delay);
      appendLog("info", `Scheduled start in ${delay}ms.`);
    },
    [appendLog],
  );

  const clearScheduledStart = useCallback(() => {
    if (scheduledStartTimerRef.current !== null) {
      window.clearTimeout(scheduledStartTimerRef.current);
      scheduledStartTimerRef.current = null;
    }
  }, []);

  const applySnapshot = useCallback(
    (message: SessionSnapshotMessage) => {
      revisionRef.current = message.snapshot_revision;
      const startAtServerMs =
        typeof message.start_at_server_ms === "number"
          ? message.start_at_server_ms
          : null;
      const shouldScheduleStart =
        message.session_state === "starting" && startAtServerMs !== null;

      if (shouldScheduleStart) {
        scheduleLocalStart(startAtServerMs);
      } else {
        clearScheduledStart();
      }

      setSession((view) => ({
        ...view,
        snapshot_revision: message.snapshot_revision,
        session_state: message.session_state,
        playback_state: message.playback_state,
        active_clip: message.active_clip,
        character_state: message.character_state,
        cooperation_level: message.cooperation_level,
        revealed_facts: message.revealed_facts,
        consecutive_fallbacks: message.consecutive_fallbacks,
        start_at_server_ms: shouldScheduleStart ? startAtServerMs : null,
        pause_reason: message.session_state === "paused" ? view.pause_reason : null,
      }));
      setCurrentClipId(message.playback_state === "playing" ? message.active_clip : null);

      if (replayAfterSnapshotRef.current) {
        replayAfterSnapshotRef.current = false;
        for (const pending of pendingRef.current.values()) {
          rawSend(pending.frame);
        }
        if (pendingRef.current.size > 0) {
          appendLog("info", `Replayed ${pendingRef.current.size} pending event(s).`);
        }
      }
    },
    [appendLog, clearScheduledStart, rawSend, scheduleLocalStart],
  );

  const applyBroadcastEnvelope = useCallback((envelope: AnyEventEnvelope) => {
    const payload = envelope.payload;
    setSession((view) => {
      const next = { ...view };
      switch (envelope.event_type) {
        case "session_started":
          next.session_state = "starting";
          break;
        case "session_paused":
          next.session_state = "paused";
          next.pause_reason = payloadString(payload, "reason");
          break;
        case "session_resumed":
          next.session_state = "active";
          next.pause_reason = null;
          next.start_at_server_ms = null;
          break;
        case "session_ended":
          next.session_state = "ended";
          break;
        case "clip_command":
        case "clip_started": {
          const clipId = payloadString(payload, "clip_id");
          next.playback_state = "playing";
          next.active_clip = clipId;
          break;
        }
        case "fallback_used": {
          const clipId = payloadString(payload, "resolved_clip_id");
          next.playback_state = "playing";
          next.active_clip = clipId;
          next.consecutive_fallbacks =
            payloadNumber(payload, "consecutive_fallbacks") ?? next.consecutive_fallbacks;
          break;
        }
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
        setCurrentClipId(payloadString(payload, "clip_id"));
        break;
      case "fallback_used":
        setCurrentClipId(payloadString(payload, "resolved_clip_id"));
        break;
      case "clip_ended":
      case "clip_interrupted":
      case "return_to_idle":
        setCurrentClipId(null);
        break;
      default:
        break;
    }
  }, []);

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
        lastAckedSequenceRef.current = Math.max(
          lastAckedSequenceRef.current,
          pending.sequence,
        );
      }
      if (message.status === "accepted" && pending !== undefined) {
        if (REVISION_BUMPING_EVENTS.has(pending.event_type)) {
          requestSnapshot();
        }
        return;
      }
      if (message.status === "rejected") {
        appendLog("warn", `Server rejected an event: ${message.reason ?? "unknown"}.`);
      }
    },
    [appendLog, requestSnapshot],
  );

  const startClockSync = useCallback(() => {
    clockSamplesRef.current = [];
    clockReportSentRef.current = false;
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
      if (clockReportSentRef.current || clockSamplesRef.current.length >= CLOCK_SYNC_SAMPLES) {
        return;
      }
      if (probeId !== null) {
        probeSentAtRef.current.delete(probeId);
      }
      const t3 = Date.now();
      const rtt = t3 - t0 - (s2 - s1);
      const offset = (s1 - t0 + (s2 - t3)) / 2;
      clockSamplesRef.current.push({ rtt_ms: rtt, offset_ms: offset });

      if (clockSamplesRef.current.length < CLOCK_SYNC_SAMPLES) {
        return;
      }
      const best = [...clockSamplesRef.current]
        .sort((a, b) => a.rtt_ms - b.rtt_ms)
        .slice(0, CLOCK_SYNC_KEEP_LOWEST);
      const offsetMs = Math.round(median(best.map((sample) => sample.offset_ms)));
      const rttMs = Math.round(median(best.map((sample) => sample.rtt_ms)));
      clockOffsetRef.current = offsetMs;
      clockReportSentRef.current = true;
      setClock({
        offset_ms: offsetMs,
        rtt_ms: rttMs,
        sample_count: CLOCK_SYNC_SAMPLES,
      });
      rawSend({
        type: "clock_offset_report",
        offset_ms: offsetMs,
        sample_count: CLOCK_SYNC_SAMPLES,
        rtt_ms: rttMs,
      });
      appendLog("info", `Clock sync complete: offset=${offsetMs}ms rtt=${rttMs}ms.`);
    },
    [appendLog, rawSend],
  );

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

  const startHeartbeat = useCallback((onTimeout: () => void) => {
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
          appendLog("error", "Heartbeat timeout; reconnecting.");
          // Do not wait for onclose: on a silent network it can come very late.
          onTimeout();
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

  const handleMessage = useCallback(
    (raw: string, socketId: number) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        appendLog("warn", "Received an unparsable WebSocket frame.");
        return;
      }

      if (isWebSocketControlMessage(parsed)) {
        const messageType = controlMessageType(parsed as unknown as Record<string, unknown>);
        switch (messageType) {
          case "pong":
            handlePong((parsed as { ping_id: string | null }).ping_id);
            return;
          case "event_ack":
            handleAck(parsed as EventAckMessage);
            return;
          case "session_snapshot":
            // The first snapshot on a socket is its admission.
            admitRef.current?.(socketId);
            applySnapshot(parsed as SessionSnapshotMessage);
            return;
          case "clock_sync_response":
            handleClockSyncResponse(
              (parsed as { probe_id: string | null }).probe_id,
              (parsed as { t0: number | null }).t0,
              (parsed as { s1: number }).s1,
              (parsed as { s2: number }).s2,
            );
            return;
          case "session_start_scheduled":
            scheduleLocalStart((parsed as { start_at_server_ms: number }).start_at_server_ms);
            return;
          case "role_connection_replaced":
            appendLog("warn", `${(parsed as { role: string }).role} connection was replaced.`);
            return;
          case "fallback_threshold_reached":
            const fallbackMessage = parsed as {
              payload: { consecutive_fallbacks: number };
            };
            setSession((view) => ({
              ...view,
              consecutive_fallbacks: fallbackMessage.payload.consecutive_fallbacks,
            }));
            appendLog(
              "warn",
              `Fallback threshold reached: ${fallbackMessage.payload.consecutive_fallbacks}.`,
            );
            return;
          default:
            return;
        }
      }

      if (isEventEnvelope(parsed)) {
        applyBroadcastEnvelope(parsed);
        if (REVISION_BUMPING_EVENTS.has(parsed.event_type)) {
          requestSnapshot();
        }
        appendLog("info", `Received ${parsed.event_type}.`);
      }
    },
    [
      appendLog,
      applyBroadcastEnvelope,
      applySnapshot,
      handleAck,
      handleClockSyncResponse,
      handlePong,
      requestSnapshot,
      scheduleLocalStart,
    ],
  );

  useEffect(() => {
    if (sessionId === null) {
      setStatus("idle");
      return;
    }

    let disposed = false;
    const lifecycle = new SocketLifecycle();
    admittedRef.current = false;

    const token = accessToken ?? readAccessToken(sessionId);
    if (!token) {
      setStatus("closed");
      setClosedReason("missing_session_token");
      return;
    }

    const clearAdmissionTimer = () => {
      if (admissionTimerRef.current !== null) {
        window.clearTimeout(admissionTimerRef.current);
        admissionTimerRef.current = null;
      }
    };

    const apply = (socket: WebSocket, socketId: number, effects: SocketEffects) => {
      if (effects.status !== undefined) {
        setStatus(effects.status);
      }
      if (effects.closedReason !== undefined) {
        setClosedReason(effects.closedReason);
      }
      if (effects.notice !== undefined) {
        setNotice(effects.notice);
      }
      if (effects.sendHello) {
        requestSnapshot();
      }
      if (effects.admitted) {
        clearAdmissionTimer();
        admittedRef.current = true;
        replayAfterSnapshotRef.current = true;
        appendLog("info", "WebSocket admitted.");
        startClockSync();
        startHeartbeat(() => apply(socket, socketId, lifecycle.timedOut(socketId)));
      }
      if (effects.detach || effects.reconnectInMs !== undefined || effects.stop) {
        clearHeartbeat();
        clearAdmissionTimer();
        admittedRef.current = false;
      }
      if (effects.detach) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
        if (socketRef.current === socket) {
          socketRef.current = null;
        }
      }
      if (effects.reconnectInMs !== undefined) {
        const delay = effects.reconnectInMs;
        appendLog(
          "warn",
          effects.notice === "occupied"
            ? `Another device holds the student role; retrying in ${delay / 1000}s.`
            : `Connection lost; retrying in ${delay / 1000}s.`,
        );
        reconnectTimerRef.current = window.setTimeout(connect, delay);
      }
      if (effects.stop) {
        appendLog("error", `Connection closed: ${effects.closedReason ?? "unknown"}.`);
      }
    };

    const connect = () => {
      reconnectTimerRef.current = null;
      if (disposed) {
        return;
      }
      const { id: socketId, status: nextStatus } = lifecycle.begin();
      setStatus(nextStatus);
      const socket = new WebSocket(sessionSocketUrl(sessionId), websocketSubprotocols(token));
      socketRef.current = socket;

      admitRef.current = (admittedId: number) => {
        if (admittedId === socketId) {
          apply(socket, socketId, lifecycle.snapshot(socketId));
        }
      };

      socket.onopen = () => {
        if (disposed || !lifecycle.isCurrent(socketId)) {
          socket.close();
          return;
        }
        // Not admitted yet: a refusal is accepted too, then closed at once.
        appendLog("info", "WebSocket open; waiting for the session snapshot.");
        admissionTimerRef.current = window.setTimeout(() => {
          admissionTimerRef.current = null;
          apply(socket, socketId, lifecycle.timedOut(socketId));
        }, ADMISSION_TIMEOUT_MS);
        apply(socket, socketId, lifecycle.opened(socketId));
      };

      socket.onmessage = (event) => {
        if (!lifecycle.isCurrent(socketId) || typeof event.data !== "string") {
          return;
        }
        handleMessage(event.data, socketId);
      };

      socket.onerror = () => {
        appendLog("warn", "WebSocket error.");
      };

      socket.onclose = (event) => {
        if (disposed || !lifecycle.isCurrent(socketId)) {
          return;
        }
        if (socketRef.current === socket) {
          socketRef.current = null;
        }
        apply(socket, socketId, lifecycle.closed(socketId, event.code, event.reason));
      };
    };

    connect();

    return () => {
      disposed = true;
      lifecycle.dispose();
      admittedRef.current = false;
      admitRef.current = null;
      clearHeartbeat();
      clearAdmissionTimer();
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (scheduledStartTimerRef.current !== null) {
        window.clearTimeout(scheduledStartTimerRef.current);
        scheduledStartTimerRef.current = null;
      }
      for (const timer of clockTimersRef.current) {
        window.clearTimeout(timer);
      }
      clockTimersRef.current = [];
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [
    accessToken,
    appendLog,
    clearHeartbeat,
    handleMessage,
    requestSnapshot,
    sessionId,
    startClockSync,
    startHeartbeat,
  ]);

  return useMemo(
    () => ({
      status,
      admitted: status === "open" || status === "degraded",
      notice,
      closedReason,
      session,
      clock,
      currentClipId,
      lastAck,
      pendingCount,
      log,
      sendClipStarted,
      sendClipEnded,
      sendRecordingStarted,
      sendRecordingStopped,
      sendRecordingFailed,
      sendChunkUploaded,
    }),
    [
      clock,
      closedReason,
      currentClipId,
      lastAck,
      log,
      notice,
      pendingCount,
      sendChunkUploaded,
      sendClipEnded,
      sendClipStarted,
      sendRecordingStarted,
      sendRecordingStopped,
      sendRecordingFailed,
      session,
      status,
    ],
  );
}
