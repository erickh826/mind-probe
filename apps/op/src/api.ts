import type {
  CreatedSession,
  JoinCodeRole,
  JoinCodesResponse,
  RecordingStatus,
  ReviewCodesResponse,
  SessionListResponse,
  UploadReissueResponse,
} from "./types";

/** A non-2xx answer. `detail` is the server's machine-readable reason. */
export class OpApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.name = "OpApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface OpClientOptions {
  baseUrl: string;
  /** The operator token. Held by the caller in memory only. */
  token: string;
  fetchImpl?: typeof fetch;
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string") {
      return detail;
    }
    // 410 recording_deleted arrives as {"error": ..., "recording_state": ...}.
    if (detail && typeof detail === "object" && "error" in detail) {
      const error = (detail as { error?: unknown }).error;
      if (typeof error === "string") {
        return error;
      }
    }
    return detail === undefined ? "" : JSON.stringify(detail);
  } catch {
    return "";
  }
}

export function createOpClient({ baseUrl, token, fetchImpl }: OpClientOptions) {
  const doFetch = fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    // Header only: the token never goes into a URL, cookie, or storage.
    headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await doFetch(`${baseUrl}${path}`, {
      ...init,
      headers,
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
    });
    if (!response.ok) {
      throw new OpApiError(response.status, await readDetail(response));
    }
    return (await response.json()) as T;
  }

  const sessionPath = (sessionId: string, suffix = "") =>
    `/sessions/${encodeURIComponent(sessionId)}${suffix}`;

  return {
    listSessions: ({ limit = 50, before }: { limit?: number; before?: string | null } = {}) => {
      const query = new URLSearchParams({ limit: String(limit) });
      if (before) {
        query.set("before", before);
      }
      return request<SessionListResponse>(`/sessions?${query}`);
    },
    listCases: () => request<{ cases: string[] }>("/cases"),
    createSession: (sessionId: string, caseId: string) =>
      request<CreatedSession>("/sessions", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, case_id: caseId }),
      }),
    recordingStatus: (sessionId: string) =>
      request<RecordingStatus>(sessionPath(sessionId, "/recording/status")),
    reissueUploadCode: (sessionId: string) =>
      request<UploadReissueResponse>(sessionPath(sessionId, "/upload-reissue"), { method: "POST" }),
    reissueJoinCodes: (sessionId: string, roles: JoinCodeRole[], revokeLive: boolean) =>
      request<JoinCodesResponse>(sessionPath(sessionId, "/join-codes"), {
        method: "POST",
        body: JSON.stringify({ roles, revoke_live: revokeLive }),
      }),
    regenerateReviewCodes: (sessionId: string) =>
      request<ReviewCodesResponse>(sessionPath(sessionId, "/review-codes"), { method: "POST" }),
  };
}

export type OpClient = ReturnType<typeof createOpClient>;
