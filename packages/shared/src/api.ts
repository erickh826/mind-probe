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

export interface ReviewLoginRequest {
  review_code: string;
  client_id?: string;
}

export interface ReviewTokenResponse {
  role: ReviewRole;
  access_token: string;
  refresh_token: string;
  expires_at: number;
}

export interface ReviewRefreshRequest {
  refresh_token: string;
}

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
