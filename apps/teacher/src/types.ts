import type { Role } from "@mind-probe/shared";

export type AuthContext = { token: string };

export interface JoinResult {
  session_id: string;
  role: Role;
  status: string;
  access_token: string;
  expires_at: number;
}

export interface SessionInfo {
  session_id: string;
  case_id: string;
  status: "created" | "starting" | "active" | "paused" | "ended" | string;
  snapshot_revision: number;
  pause_reason: string | null;
  start_at_ms: number | null;
  created_at: string | null;
  ended_at: string | null;
}

export interface ArtefactInfo {
  exists: boolean;
  size_bytes: number | null;
  path?: string | null;
}

export interface RecordingStatus {
  session_id: string;
  chunk_count: number;
  first_sequence: number | null;
  last_sequence: number | null;
  missing_sequences: number[];
  total_bytes: number;
  header_chunk_ok: boolean;
  metadata_rows: number;
  raw_webm: ArtefactInfo;
  mp4: ArtefactInfo;
}

export interface TimelineSummary {
  entry_count: number;
  entries_by_kind: Record<string, number>;
  wizard_actions_by_type: Record<string, number>;
  clip_count: number;
  recording_span_count: number;
  first_t_ms: number | null;
  last_t_ms: number | null;
}

export interface TimelineBaseEntry {
  t_ms: number | null;
  sequence: number;
  source: string;
  event_type: string;
  server_timestamp_ms: number | null;
  client_timestamp_ms: number;
  clock_offset_ms: number | null;
  payload: Record<string, unknown>;
}

export interface TimelineClipMetadata {
  text?: string;
  intent?: string;
  category?: string;
  character_state?: string;
  manifest_duration_ms?: number;
  duration_ms?: number;
  file?: string;
}

export interface ClipManifestEntry extends TimelineClipMetadata {
  clip_id: string;
  file: string;
}

export interface ClipManifestResponse {
  case_id: string;
  clips: ClipManifestEntry[];
}

export interface TeacherMarker {
  id: string;
  t_ms: number;
  note: string;
  created_at: string;
}

export interface ClipTimelineEntry extends TimelineBaseEntry {
  kind: "clip";
  clip_id: string | null;
  clip: TimelineClipMetadata | null;
  started: boolean;
  end_t_ms: number | null;
  end_event_type: string | null;
  duration_ms: number | null;
  played_duration_ms: number | null;
  interrupt_reason?: string;
}

export interface WizardActionEntry extends TimelineBaseEntry {
  kind: "wizard_action";
  action: string;
  clip_id?: string;
  clip?: TimelineClipMetadata | null;
  reason?: string;
  fallback_category?: string;
  resolved_clip_id?: string;
  consecutive_fallbacks?: number;
  question_text?: string;
  note?: string;
}

export interface SyncTimelineEntry extends TimelineBaseEntry {
  kind: "sync";
}

export interface LifecycleTimelineEntry extends TimelineBaseEntry {
  kind: "lifecycle";
}

export type TimelineEntry =
  | ClipTimelineEntry
  | WizardActionEntry
  | SyncTimelineEntry
  | LifecycleTimelineEntry;

export interface RecordingSpan {
  kind: "recording_span";
  time_base: "client_relative_to_session_start" | string;
  first_sequence: number;
  last_sequence: number;
  chunk_count: number;
  start_ms: number | null;
  end_ms: number | null;
  size_bytes: number;
}

export interface TimelineResponse {
  session_id: string;
  case_id: string;
  status: string;
  snapshot_revision: number;
  t0_server_timestamp_ms: number | null;
  t0_source: string;
  scheduled_start_at_monotonic_ms: number | null;
  event_count: number;
  entries: TimelineEntry[];
  recording_spans: RecordingSpan[];
  summary: TimelineSummary;
}

export interface RubricCategory {
  key: string;
  label: string;
  max: number;
}

export interface RubricData {
  case_id: string;
  description?: string;
  total: number;
  categories: RubricCategory[];
}

export interface RubricResponse {
  case_id: string;
  rubric: RubricData;
}

export interface RubricScoreItem {
  key: string;
  label: string;
  score: number;
  max: number;
  comment: string;
}

export interface RubricEvaluationReport {
  session_id: string;
  case_id: string;
  exported_at: string;
  total_score: number;
  total_possible: number;
  items: RubricScoreItem[];
  overall_comment: string;
  teacher_markers: TeacherMarker[];
}

export interface AuditEntry {
  id: number;
  action: string;
  actor_role: string | null;
  created_at: string | null;
  details: Record<string, unknown>;
  token_hash_prefix: string | null;
}

export interface AuditResponse {
  session_id: string;
  entries: AuditEntry[];
}
