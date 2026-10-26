// REST contract for ADR-0005 (auth, recording integrity, deletion). Keep in sync with server/app pydantic models.

export type TokenType = "live" | "review";
export type ReviewRole = "teacher" | "observer";
export type TokenRevokeReason = "session_ended" | "security";

export interface JoinResponse {
  session_id: string;
  role: string;
  status: "ok";
  access_token: string;
  expires_at: number;
}

/** Plaintext review codes. Returned to the operator once; only hashes are stored. */
export interface ReviewCodes {
  teacher: string;
  observer: string;
}

export interface ReviewCodesResponse {
  session_id: string;
  review_codes: ReviewCodes;
}

export interface ReviewLoginRequest {
  review_code: string;
  client_id?: string;
}

/**
 * Access token expires at `expires_at` (unix seconds).
 * The refresh token is opaque and lives for `MINDPROBE_REVIEW_REFRESH_TTL`.
 */
export interface ReviewTokenResponse {
  role: ReviewRole;
  access_token: string;
  refresh_token: string;
  expires_at: number;
}

export interface ReviewRefreshRequest {
  refresh_token: string;
}

export interface ReviewRevokeResponse {
  status: "ok";
  revoked_grants: number;
}

export type ReviewAuditAction =
  | "review_login"
  | "review_refresh"
  | "review_logout"
  | "review_revoke";

export type RecordingState =
  | "recording"
  | "finalizing"
  | "complete"
  | "incomplete"
  | "transcoding"
  | "transcoded"
  | "transcode_failed"
  | "uploaded"
  | "deleting"
  | "deleted";

export type FinalizeState = "none" | "pending" | "complete" | "incomplete";

export type IncompleteReason = "recorder_lost" | "missing_chunks" | "operator";

export interface FinalizeRequest {
  final_sequence: number;
  total_chunks: number;
  total_duration_ms: number;
  reason?: "recorder_lost";
}

export interface FinalizeResponse {
  finalize_state: FinalizeState;
  missing_sequences: number[];
}

/** Student-facing, deliberately minimal (GET /recording/{id}/status). */
export interface StudentRecordingStatus {
  received_sequences: number[];
  next_expected_sequence: number | null;
  final_sequence: number | null;
  finalize_state: FinalizeState;
  recording_state: RecordingState;
}

export interface DeleteRecordingRequest {
  confirm: string;
}

export interface RecordingJobResponse {
  session_id: string;
  status: "queued";
  job_id: number;
}

export interface RecordingPlaybackResponse {
  session_id: string;
  url: string;
  expires_in: number;
  recording_state: RecordingState;
}

export interface DeleteRecordingResponse {
  session_id: string;
  recording_state: "deleting" | "deleted";
}
