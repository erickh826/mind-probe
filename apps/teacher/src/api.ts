import { apiUrl } from "./config";
import type {
  AuditResponse,
  AuthContext,
  ClipManifestEntry,
  ClipManifestResponse,
  JoinResult,
  RecordingStatus,
  RubricData,
  RubricResponse,
  SessionInfo,
  TimelineResponse,
} from "./types";

function authHeaders(auth?: AuthContext): HeadersInit {
  if (auth?.mode !== "bearer") {
    return {};
  }
  return { Authorization: `Bearer ${auth.token}` };
}

async function fetchJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(apiUrl(path), {
    ...init,
    headers,
    credentials: "include",
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        message = body.detail;
      } else if (body.detail) {
        message = JSON.stringify(body.detail);
      }
    } catch {
      const text = await response.text();
      if (text) {
        message = text;
      }
    }
    throw new Error(message);
  }

  return response.json() as Promise<T>;
}

export function joinSession(sessionId: string, joinCode: string, clientId: string): Promise<JoinResult> {
  return fetchJson<JoinResult>(`/sessions/${encodeURIComponent(sessionId)}/join`, {
    method: "POST",
    body: JSON.stringify({ join_code: joinCode, client_id: clientId }),
  });
}

export function fetchSession(sessionId: string): Promise<SessionInfo> {
  return fetchJson<SessionInfo>(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function fetchTimeline(sessionId: string, auth: AuthContext): Promise<TimelineResponse> {
  return fetchJson<TimelineResponse>(`/sessions/${encodeURIComponent(sessionId)}/timeline`, {
    headers: authHeaders(auth),
  });
}

export function fetchRecordingStatus(sessionId: string, auth: AuthContext): Promise<RecordingStatus> {
  return fetchJson<RecordingStatus>(`/sessions/${encodeURIComponent(sessionId)}/recording/status`, {
    headers: authHeaders(auth),
  });
}

export async function fetchRubric(caseId: string): Promise<RubricData> {
  const response = await fetchJson<RubricResponse>(`/cases/${encodeURIComponent(caseId)}/rubric`);
  return response.rubric;
}

export async function fetchClipManifest(caseId: string): Promise<ClipManifestEntry[]> {
  const response = await fetchJson<ClipManifestResponse>(`/cases/${encodeURIComponent(caseId)}/clips`);
  return response.clips;
}

export function fetchAuditLogs(sessionId: string, auth: AuthContext): Promise<AuditResponse> {
  return fetchJson<AuditResponse>(`/sessions/${encodeURIComponent(sessionId)}/audit`, {
    headers: authHeaders(auth),
  });
}

export function fetchRecordingBlob(sessionId: string, auth: AuthContext, signal: AbortSignal): Promise<Blob> {
  return fetch(apiUrl(`/sessions/${encodeURIComponent(sessionId)}/recording`), {
    credentials: "include",
    headers: authHeaders(auth),
    signal,
  }).then((response) => {
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    return response.blob();
  });
}
