// Response shapes for the operator-only endpoints in server/app/main.py.

export type JoinCodeRole = "student" | "wizard" | "teacher";
export type ReviewCodeRole = "teacher" | "observer";

export interface SessionInfo {
  session_id: string;
  case_id: string;
  status: string;
  snapshot_revision: number;
  pause_reason: string | null;
  start_at_ms: number | null;
  created_at: string | null;
  ended_at: string | null;
}

/** One row of `GET /sessions`. */
export interface OperatorSessionRow extends SessionInfo {
  recording_state: string | null;
  finalize_state: string | null;
  incomplete_reason: string | null;
  needs_operator: boolean;
  stored_sequences: number;
  seconds_since_last_chunk: number | null;
  upload_grace_ends_at: string | null;
  upload_reissue_refusal: string | null;
}

export interface SessionListResponse {
  sessions: OperatorSessionRow[];
  /** Pass back as `before` for the next older page; null on the last page. */
  next_cursor: string | null;
  upload_grace_seconds: number;
}

export interface CreatedSession extends SessionInfo {
  join_codes: Record<JoinCodeRole, string>;
  review_codes: Record<ReviewCodeRole, string>;
}

/** `POST /sessions/{id}/join-codes`: only the roles that were reissued. */
export interface JoinCodesResponse {
  session_id: string;
  join_codes: Partial<Record<JoinCodeRole, string>>;
  revoke_live: boolean;
  revoked_tokens: number;
}

export interface ReviewCodesResponse {
  session_id: string;
  review_codes: Record<ReviewCodeRole, string>;
}

export interface UploadReissueResponse {
  upload_code: string;
  expires_at: string;
  recording_state: string | null;
  stored_sequences: number;
  seconds_since_last_chunk: number | null;
}

export interface ArtefactInfo {
  exists: boolean;
  size_bytes: number | null;
}

/** `GET /sessions/{id}/recording/status`. */
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
  recording_state?: string;
  finalize_state?: string;
  final_sequence?: number | null;
  incomplete_reason?: string | null;
}

/** Plaintext codes the server returned once; kept only in this tab's memory. */
export interface HeldCodes {
  join?: Partial<Record<JoinCodeRole, string>>;
  review?: Partial<Record<ReviewCodeRole, string>>;
  upload?: { code: string; expiresAt: string };
}
