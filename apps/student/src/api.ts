import type { ClipManifestEntry, Role } from "@mind-probe/shared";
import { apiUrl } from "./config";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), { credentials: "include", ...init });
  } catch (cause) {
    throw new ApiError(0, `Session Server is unreachable: ${String(cause)}`);
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      isRecord(body) && typeof body.detail === "string"
        ? body.detail
        : `HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }
  return body;
}

export interface JoinResult {
  session_id: string;
  role: Role | string;
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
  if (!isRecord(body)) {
    throw new ApiError(500, "Join response has an unexpected shape.");
  }
  return {
    session_id: String(body.session_id ?? sessionId),
    role: typeof body.role === "string" ? body.role : "unknown",
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

export async function uploadRecordingChunk(
  sessionId: string,
  sequence: number,
  chunk: Blob,
): Promise<void> {
  const form = new FormData();
  form.append("sequence", String(sequence));
  form.append("chunk", chunk, `${String(sequence).padStart(6, "0")}.webm`);

  await request(`/recording/${encodeURIComponent(sessionId)}/chunk`, {
    method: "POST",
    body: form,
  });
}
