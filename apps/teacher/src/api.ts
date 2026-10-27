import {
  authorizedReviewFetch,
  clearReviewSession,
  isExpiredReviewDetail,
  isUnrecoverableReviewDetail,
  readReviewSession,
  rememberAccessToken,
  rememberReviewSession,
  type ReviewTokenResponse,
  type RecordingPlaybackResponse,
  type DeleteRecordingResponse,
} from "@mind-probe/shared";
import { apiUrl } from "./config";
import { ReviewRequestError } from "./reviewErrors";
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

async function responseError(response: Response): Promise<ReviewRequestError> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) {
      return new ReviewRequestError(body.detail);
    }
    if (body.detail) {
      const encoded = JSON.stringify(body.detail);
      return new ReviewRequestError(encoded, encoded);
    }
  } catch {
    const text = await response.text();
    if (text) {
      return new ReviewRequestError(text, text);
    }
  }
  const fallback = `${response.status} ${response.statusText}`.trim();
  return new ReviewRequestError(fallback, fallback);
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
      throw await responseError(response);
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
        // Chromium stores a 410 with no cache policy and replays "deleting"
        // forever. Bypass the HTTP cache on every review call.
        cache: "no-store",
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
    throw await responseError(response);
  }
  return response.json() as Promise<T>;
}

export async function reviewLogout(sessionId: string, auth: AuthContext): Promise<void> {
  const response = await send(`/sessions/${encodeURIComponent(sessionId)}/review-logout`, {
    method: "POST",
    headers: authHeaders(auth),
  });
  if (response.ok || response.status === 401) {
    clearReviewSession();
    return;
  }
  throw await responseError(response);
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

export async function fetchRecordingStatus(sessionId: string, auth: AuthContext): Promise<RecordingStatus> {
  const response = await send(`/sessions/${encodeURIComponent(sessionId)}/recording/status`, {
    headers: authHeaders(auth),
  });
  if (response.status === 410) {
    const body = await response.json() as { detail: { recording_state: "deleting" | "deleted" } };
    return { session_id: sessionId, recording_state: body.detail.recording_state,
      chunk_count: 0, first_sequence: null, last_sequence: null, missing_sequences: [],
      total_bytes: 0, header_chunk_ok: false, metadata_rows: 0,
      raw_webm: { exists: false, size_bytes: null }, mp4: { exists: false, size_bytes: null } };
  }
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<RecordingStatus>;
}

export function deleteRecording(sessionId: string, confirm: string, auth: AuthContext): Promise<DeleteRecordingResponse> {
  return fetchJson(`/sessions/${encodeURIComponent(sessionId)}/recording`, {
    method: "DELETE", headers: authHeaders(auth), body: JSON.stringify({ confirm }),
  });
}

export async function fetchRecordingPlayback(sessionId: string, auth: AuthContext, signal: AbortSignal): Promise<string> {
  while (!signal.aborted) {
    const response = await send(`/sessions/${encodeURIComponent(sessionId)}/recording`, {
      headers: authHeaders(auth), signal,
    });
    if (response.status === 202) {
      await new Promise<void>((resolve, reject) => {
        const onAbort = () => { window.clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
        const timer = window.setTimeout(() => { signal.removeEventListener("abort", onAbort); resolve(); }, 2000);
        signal.addEventListener("abort", onAbort, { once: true });
        if (signal.aborted) onAbort();
      });
      continue;
    }
    if (!response.ok) throw await responseError(response);
    const body = await response.json() as RecordingPlaybackResponse;
    return /^https?:\/\//i.test(body.url) ? body.url : apiUrl(body.url);
  }
  throw new DOMException("Aborted", "AbortError");
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
    if (response.status === 401) {
      const detail = await readDetail(response);
      if (isUnrecoverableReviewDetail(detail) || isExpiredReviewDetail(detail)) {
        throw new ReviewRequestError(detail);
      }
    }
    return null;
  }
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.json() as Promise<AuditResponse>;
}

export async function fetchRecordingBlob(
  sessionId: string,
  auth: AuthContext,
  signal: AbortSignal,
): Promise<Blob> {
  const url = await fetchRecordingPlayback(sessionId, auth, signal);
  const response = await fetch(url, { credentials: "omit", signal });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.blob();
}
