import type {
  CharacterState,
  ClipManifestEntry,
  PauseReason,
  PlaybackState,
  SessionState,
} from "@mind-probe/shared";

export const STUDENT_ROLE = "student" as const;

export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "open"
  | "degraded"
  | "reconnecting"
  | "closed";

export interface SessionView {
  snapshot_revision: number;
  session_state: SessionState;
  playback_state: PlaybackState;
  active_clip: string | null;
  character_state: CharacterState | string;
  cooperation_level: number;
  revealed_facts: string[];
  consecutive_fallbacks: number;
  start_at_server_ms: number | null;
  pause_reason: PauseReason | string | null;
}

export const INITIAL_SESSION_VIEW: SessionView = {
  snapshot_revision: 0,
  session_state: "created",
  playback_state: "idle",
  active_clip: null,
  character_state: "neutral",
  cooperation_level: 0,
  revealed_facts: [],
  consecutive_fallbacks: 0,
  start_at_server_ms: null,
  pause_reason: null,
};

export interface ClockState {
  offset_ms: number | null;
  rtt_ms: number | null;
  sample_count: number;
}

export type RecorderStatus =
  | "idle"
  | "requesting"
  | "recording"
  | "stopping"
  | "stopped"
  | "error"
  | "unavailable";

export interface QueuedRecordingChunk {
  chunk_index: number;
  timestamp_ms: number;
  blob: Blob;
  status: "queued" | "uploading" | "uploaded" | "failed";
  attempts: number;
}

export interface PlaybackClip {
  clipId: string;
  src: string | null;
  label: string;
  loop: boolean;
}

export interface JoinedSession {
  sessionId: string;
  caseId: string;
  clips: ClipManifestEntry[];
}

export type ConsoleLogLevel = "info" | "warn" | "error";

export interface ConsoleLogEntry {
  id: string;
  at_ms: number;
  level: ConsoleLogLevel;
  text: string;
}
