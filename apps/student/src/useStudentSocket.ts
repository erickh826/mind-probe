import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  controlMessageType,
  isEventEnvelope,
  isWebSocketControlMessage,
} from "@mind-probe/shared";
import type {
  AnyEventEnvelope,
  BaseEventPayload,
  EventAckMessage,
  EventType,
  SessionSnapshotMessage,
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
const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
const CLOCK_SYNC_SAMPLES = 9;
const CLOCK_SYNC_KEEP_LOWEST = 5;
const CLOCK_SYNC_SPACING_MS = 120;
const MAX_LOG_ENTRIES = 80;
const TERMINAL_CLOSE_CODES = new Set([4400, 4401, 4404, 4409]);

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

export function useStudentSocket(sessionId: string | null): StudentSocket {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [closedReason, setClosedReason] = useState<string | null>(null);
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

      if (!rawSend(envelope)) {
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
          appendLog("error", "Heartbeat timeout; reconnecting.");
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

  const handleMessage = useCallback(
    (raw: string) => {
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
        replayAfterSnapshotRef.current = true;
        appendLog("info", "WebSocket connected.");
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
        appendLog("warn", "WebSocket error.");
      };

      socket.onclose = (event) => {
        clearHeartbeat();
        socketRef.current = null;
        if (disposed) {
          return;
        }
        if (TERMINAL_CLOSE_CODES.has(event.code)) {
          shouldReconnectRef.current = false;
          setStatus("closed");
          setClosedReason(event.reason || String(event.code));
          appendLog("error", `Connection refused (${event.code}): ${event.reason || "unknown"}.`);
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
        appendLog("warn", `Connection lost; retrying in ${delay / 1000}s.`);
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
      sendChunkUploaded,
    }),
    [
      clock,
      closedReason,
      currentClipId,
      lastAck,
      log,
      pendingCount,
      sendChunkUploaded,
      sendClipEnded,
      sendClipStarted,
      sendRecordingStarted,
      sendRecordingStopped,
      session,
      status,
    ],
  );
}
