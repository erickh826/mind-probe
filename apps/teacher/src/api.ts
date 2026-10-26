import {
  authorizedReviewFetch,
  clearReviewSession,
  readReviewSession,
  rememberAccessToken,
  rememberReviewSession,
  type ReviewTokenResponse,
} from "@mind-probe/shared";
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
  if (!auth?.token) {
    return {};
  }
  return { Authorization: `Bearer ${auth.token}` };
}

function sessionIdFromPath(path: string): string | null {
  const match = /^\/sessions\/([^/?#]+)/.exec(path);
  if (!match?.[1]) {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function bearerFrom(headers: Headers): string {
  const value = headers.get("Authorization") ?? "";
  const prefix = "bearer ";
  if (value.slice(0, prefix.length).toLowerCase() !== prefix) {
    return "";
  }
  return value.slice(prefix.length).trim();
}

async function errorMessage(response: Response): Promise<string> {
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
  return message;
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : "";
  } catch {
    return "";
  }
}

function postReviewRefresh(sessionId: string, refreshToken: string): Promise<ReviewTokenResponse> {
  return fetch(apiUrl(`/sessions/${encodeURIComponent(sessionId)}/review-refresh`), {
    method: "POST",
    credentials: "omit",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  }).then(async (response) => {
    if (!response.ok) {
      throw new Error(await errorMessage(response));
    }
    return response.json() as Promise<ReviewTokenResponse>;
  });
}

async function send(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const requestedToken = bearerFrom(headers);
  const sessionId = sessionIdFromPath(path);
  return authorizedReviewFetch({
    requestedToken,
    sessionId,
    perform: (accessToken) => {
      const nextHeaders = new Headers(headers);
      if (accessToken) {
        nextHeaders.set("Authorization", `Bearer ${accessToken}`);
      }
      return fetch(apiUrl(path), {
        ...init,
        headers: nextHeaders,
        credentials: "omit",
      });
    },
    refresh: (refreshToken) => {
      const stored = readReviewSession();
      const id = stored?.sessionId ?? sessionId;
      if (!id) {
        return Promise.reject(new Error("review session missing"));
      }
      return postReviewRefresh(id, refreshToken);
    },
    readDetail,
  });
}

async function fetchJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await send(path, init);
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json() as Promise<T>;
}

export async function joinSession(
  sessionId: string,
  joinCode: string,
  clientId: string,
): Promise<JoinResult> {
  clearReviewSession();
  const joined = await fetchJson<JoinResult>(`/sessions/${encodeURIComponent(sessionId)}/join`, {
    method: "POST",
    body: JSON.stringify({ join_code: joinCode, client_id: clientId }),
  });
  if (!joined.access_token) {
    throw new Error("Join response did not include an access token.");
  }
  rememberAccessToken(joined.session_id || sessionId, joined.access_token);
  return joined;
}

export async function reviewLogin(
  sessionId: string,
  reviewCode: string,
  clientId: string,
): Promise<ReviewTokenResponse> {
  clearReviewSession();
  const logged = await fetchJson<ReviewTokenResponse>(
    `/sessions/${encodeURIComponent(sessionId)}/review-login`,
    {
      method: "POST",
      body: JSON.stringify({ review_code: reviewCode, client_id: clientId }),
    },
  );
  if (!logged.access_token || !logged.refresh_token) {
    throw new Error("Review login did not include tokens.");
  }
  rememberReviewSession({
    sessionId,
    role: logged.role,
    accessToken: logged.access_token,
    refreshToken: logged.refresh_token,
    expiresAt: logged.expires_at,
  });
  rememberAccessToken(sessionId, logged.access_token);
  return logged;
}

export function fetchSession(sessionId: string, auth: AuthContext): Promise<SessionInfo> {
  return fetchJson<SessionInfo>(`/sessions/${encodeURIComponent(sessionId)}`, {
    headers: authHeaders(auth),
  });
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

export async function fetchRubric(caseId: string, auth: AuthContext): Promise<RubricData> {
  const response = await fetchJson<RubricResponse>(`/cases/${encodeURIComponent(caseId)}/rubric`, {
    headers: authHeaders(auth),
  });
  return response.rubric;
}

export async function fetchClipManifest(caseId: string, auth: AuthContext): Promise<ClipManifestEntry[]> {
  const response = await fetchJson<ClipManifestResponse>(`/cases/${encodeURIComponent(caseId)}/clips`, {
    headers: authHeaders(auth),
  });
  return response.clips;
}

export async function fetchAuditLogs(
  sessionId: string,
  auth: AuthContext,
): Promise<AuditResponse | null> {
  // Observer and live tokens are refused. That refusal must not fail the rest of the review load.
  const response = await send(`/sessions/${encodeURIComponent(sessionId)}/audit`, {
    headers: authHeaders(auth),
  });
  if (response.status === 401 || response.status === 403) {
    return null;
  }
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  return response.json() as Promise<AuditResponse>;
}

export async function fetchRecordingBlob(
  sessionId: string,
  auth: AuthContext,
  signal: AbortSignal,
): Promise<Blob> {
  const response = await send(`/sessions/${encodeURIComponent(sessionId)}/recording`, {
    headers: authHeaders(auth),
    signal,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.blob();
}
