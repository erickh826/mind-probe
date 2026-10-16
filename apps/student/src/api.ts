import type { ClipManifestEntry, Role } from "@mind-probe/shared";
import { currentAccessToken, rememberAccessToken } from "@mind-probe/shared";
import { ApiError } from "./apiError";
import { apiUrl } from "./config";

export { ApiError };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const headers = new Headers(init?.headers);
  const token = currentAccessToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers,
      credentials: "omit",
    });
  } catch (cause) {
    throw new ApiError(0, `Session Server is unreachable: ${String(cause)}`);
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const parsed = errorDetail(body, response.status);
    throw new ApiError(response.status, parsed.message, parsed.code);
  }
  return body;
}

function errorDetail(body: unknown, status: number): { message: string; code: string | null } {
  if (!isRecord(body)) {
    return { message: `HTTP ${status}`, code: null };
  }
  const detail = body.detail;
  if (typeof detail === "string") {
    return { message: detail, code: detail };
  }
  if (isRecord(detail)) {
    const code = typeof detail.error === "string" ? detail.error : null;
    const message =
      typeof detail.message === "string" ? detail.message : code ?? `HTTP ${status}`;
    return { message, code };
  }
  return { message: `HTTP ${status}`, code: null };
}

export interface JoinResult {
  session_id: string;
  role: Role | string;
  access_token: string;
  expires_at: number;
}

export async function joinSession(
  sessionId: string,
  joinCode: string,
  clientId?: string,
): Promise<JoinResult> {
  const body = await request(`/sessions/${encodeURIComponent(sessionId)}/join`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      join_code: joinCode,
      ...(clientId ? { client_id: clientId } : {}),
    }),
  });
  if (!isRecord(body) || typeof body.access_token !== "string" || body.access_token.length === 0) {
    throw new ApiError(500, "Join response did not include an access token.");
  }
  const resolvedSessionId = String(body.session_id ?? sessionId);
  rememberAccessToken(resolvedSessionId, body.access_token);
  return {
    session_id: resolvedSessionId,
    role: typeof body.role === "string" ? body.role : "unknown",
    access_token: body.access_token,
    expires_at: typeof body.expires_at === "number" ? body.expires_at : 0,
  };
}

export interface SessionInfo {
  session_id: string;
  case_id: string;
  status: string;
}

export async function fetchSession(sessionId: string): Promise<SessionInfo> {
  const body = await request(`/sessions/${encodeURIComponent(sessionId)}`);
  if (!isRecord(body)) {
    throw new ApiError(500, "Session response has an unexpected shape.");
  }
  return {
    session_id: String(body.session_id ?? sessionId),
    case_id: String(body.case_id ?? ""),
    status: String(body.status ?? "unknown"),
  };
}

export async function fetchClipManifest(caseId: string): Promise<ClipManifestEntry[]> {
  const body = await request(`/cases/${encodeURIComponent(caseId)}/clips`);
  if (!isRecord(body) || !Array.isArray(body.clips)) {
    return [];
  }
  return body.clips.flatMap((item): ClipManifestEntry[] => {
    if (!isRecord(item) || typeof item.clip_id !== "string" || typeof item.file !== "string") {
      return [];
    }
    return [
      {
        clip_id: item.clip_id,
        category: String(item.category ?? ""),
        intent: String(item.intent ?? ""),
        character_state: String(item.character_state ?? "neutral"),
        text: String(item.text ?? ""),
        reveals: Array.isArray(item.reveals) ? item.reveals.map(String) : [],
        requires: Array.isArray(item.requires) ? item.requires.map(String) : [],
        forbidden_after: Array.isArray(item.forbidden_after)
          ? item.forbidden_after.map(String)
          : [],
        duration_ms:
          typeof item.duration_ms === "number" && Number.isFinite(item.duration_ms)
            ? item.duration_ms
            : 0,
        file: item.file,
        hotkey:
          typeof item.hotkey === "string" || item.hotkey === null ? item.hotkey : null,
      },
    ];
  });
}

export interface ChunkAck {
  status: string;
}

export async function uploadRecordingChunk(
  sessionId: string,
  sequence: number,
  chunk: Blob,
  meta: { start_ms: number; end_ms: number; checksum: string },
): Promise<ChunkAck> {
  const form = new FormData();
  form.append("sequence", String(sequence));
  form.append("start_ms", String(meta.start_ms));
  form.append("end_ms", String(meta.end_ms));
  form.append("checksum", meta.checksum);
  form.append("chunk", chunk, `${String(sequence).padStart(6, "0")}.webm`);

  const body = await request(`/recording/${encodeURIComponent(sessionId)}/chunk`, {
    method: "POST",
    body: form,
  });
  if (isRecord(body) && typeof body.status === "string") {
    return { status: body.status };
  }
  return { status: "received" };
}

export interface UploadStatus {
  received_sequences: number[];
  next_expected_sequence: number | null;
  final_sequence: number | null;
  finalize_state: "none" | "pending" | "complete" | "incomplete";
  recording_state: string;
}

function asNumberList(value: unknown): number[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is number => typeof item === "number" && Number.isFinite(item));
}

export async function fetchUploadStatus(sessionId: string): Promise<UploadStatus> {
  const body = await request(`/recording/${encodeURIComponent(sessionId)}/status`);
  if (!isRecord(body)) {
    throw new ApiError(500, "Upload status response has an unexpected shape.");
  }
  const finalize = body.finalize_state;
  const finalizeState =
    finalize === "pending" || finalize === "complete" || finalize === "incomplete"
      ? finalize
      : "none";
  return {
    received_sequences: asNumberList(body.received_sequences),
    next_expected_sequence:
      typeof body.next_expected_sequence === "number" ? body.next_expected_sequence : null,
    final_sequence: typeof body.final_sequence === "number" ? body.final_sequence : null,
    finalize_state: finalizeState,
    recording_state: typeof body.recording_state === "string" ? body.recording_state : "recording",
  };
}

export interface FinalizeResult {
  recording_state: string;
  finalize_state: "none" | "pending" | "complete" | "incomplete";
  final_sequence: number | null;
  missing_sequences: number[];
}

export async function finalizeRecording(
  sessionId: string,
  payload: {
    final_sequence: number;
    total_chunks: number;
    total_duration_ms: number;
    reason?: "recorder_lost";
  },
): Promise<FinalizeResult> {
  const body = await request(`/recording/${encodeURIComponent(sessionId)}/finalize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!isRecord(body)) {
    throw new ApiError(500, "Finalize response has an unexpected shape.");
  }
  const finalize = body.finalize_state;
  const finalizeState =
    finalize === "pending" || finalize === "complete" || finalize === "incomplete"
      ? finalize
      : "none";
  return {
    recording_state: typeof body.recording_state === "string" ? body.recording_state : "",
    finalize_state: finalizeState,
    final_sequence: typeof body.final_sequence === "number" ? body.final_sequence : null,
    missing_sequences: asNumberList(body.missing_sequences),
  };
}
