// Review-grant credentials for the teacher app (spec revision 11, ADR-0005 §6.5).
// Access and refresh tokens stay in memory and sessionStorage. They are not
// cookies, and they are not the student typ=upload credential.

import type { ReviewRole, ReviewTokenResponse } from "./api";

export interface StoredReviewSession {
  sessionId: string;
  role: ReviewRole;
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
}

const REVIEW_SESSION_KEY = "mindprobe.review_session";

let memory: StoredReviewSession | null = null;
const knownAccessTokens = new Set<string>();
let refreshFlight: Promise<StoredReviewSession> | null = null;

export type ReviewFetch = (input: string, init?: RequestInit) => Promise<Response>;

function writeStorage(key: string, value: string): void {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // Private-mode storage can reject the write. Memory still authorizes this document.
  }
}

function readStorage(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function removeStorage(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // Ignore storage that is unavailable.
  }
}

function isReviewRole(value: unknown): value is ReviewRole {
  return value === "teacher" || value === "observer";
}

function isStoredReviewSession(value: unknown): value is StoredReviewSession {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Partial<StoredReviewSession>;
  return (
    typeof record.sessionId === "string" &&
    record.sessionId.length > 0 &&
    isReviewRole(record.role) &&
    typeof record.accessToken === "string" &&
    record.accessToken.length > 0 &&
    typeof record.refreshToken === "string" &&
    record.refreshToken.length > 0 &&
    typeof record.expiresAt === "number" &&
    Number.isFinite(record.expiresAt)
  );
}

function hold(session: StoredReviewSession): StoredReviewSession {
  memory = session;
  knownAccessTokens.add(session.accessToken);
  return session;
}

/** Keep the review login in memory and sessionStorage. */
export function rememberReviewSession(session: StoredReviewSession): StoredReviewSession {
  const stored = hold(session);
  writeStorage(REVIEW_SESSION_KEY, JSON.stringify(stored));
  return stored;
}

export function readReviewSession(): StoredReviewSession | null {
  if (memory) {
    return memory;
  }
  const raw = readStorage(REVIEW_SESSION_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isStoredReviewSession(parsed)) {
      return null;
    }
    return hold(parsed);
  } catch {
    return null;
  }
}

/** Drop the review login so the next request cannot refresh it. */
export function clearReviewSession(): void {
  memory = null;
  knownAccessTokens.clear();
  removeStorage(REVIEW_SESSION_KEY);
}

/** Test hook: drop every in-memory review credential, including a refresh in flight. */
export function resetReviewSessionStateForTests(): void {
  clearReviewSession();
  refreshFlight = null;
}

/** Test hook: force the next read to come from sessionStorage. */
export function forgetReviewMemoryForTests(): void {
  memory = null;
}

export function isExpiredReviewDetail(detail: string): boolean {
  return detail === "expired_session_token" || detail === "expired";
}

function sessionForToken(requestedToken: string): StoredReviewSession | null {
  if (!requestedToken) {
    return null;
  }
  const stored = readReviewSession();
  if (!stored) {
    return null;
  }
  if (requestedToken === stored.accessToken || knownAccessTokens.has(requestedToken)) {
    return stored;
  }
  return null;
}

function refreshReviewSession(
  expected: StoredReviewSession,
  refresh: (refreshToken: string) => Promise<ReviewTokenResponse>,
): Promise<StoredReviewSession> {
  const current = readReviewSession();
  if (current && current.refreshToken !== expected.refreshToken) {
    return Promise.resolve(current);
  }
  if (refreshFlight) {
    return refreshFlight;
  }
  const flight = (async () => {
    const latest = readReviewSession();
    if (!latest) {
      throw new Error("review session missing");
    }
    try {
      const body = await refresh(latest.refreshToken);
      if (
        !body.access_token ||
        !body.refresh_token ||
        !isReviewRole(body.role) ||
        typeof body.expires_at !== "number"
      ) {
        throw new Error("review refresh incomplete");
      }
      return rememberReviewSession({
        sessionId: latest.sessionId,
        role: body.role,
        accessToken: body.access_token,
        refreshToken: body.refresh_token,
        expiresAt: body.expires_at,
      });
    } catch (error) {
      clearReviewSession();
      throw error;
    }
  })();
  refreshFlight = flight;
  void flight.finally(() => {
    if (refreshFlight === flight) {
      refreshFlight = null;
    }
  });
  return flight;
}

/**
 * Call `perform` with the latest review access token. A 401 `expired_session_token`
 * refreshes once (single-flight) and retries. Other 401s are returned as-is.
 * `sessionId` skips refresh when the path names a different session.
 */
export async function authorizedReviewFetch(args: {
  requestedToken: string;
  sessionId?: string | null;
  perform: (accessToken: string) => Promise<Response>;
  refresh: (refreshToken: string) => Promise<ReviewTokenResponse>;
  readDetail: (response: Response) => Promise<string>;
}): Promise<Response> {
  const stored = sessionForToken(args.requestedToken);
  if (stored && args.sessionId && stored.sessionId !== args.sessionId) {
    return args.perform(args.requestedToken);
  }
  const token = stored?.accessToken ?? args.requestedToken;
  const response = await args.perform(token);
  if (!stored || response.status !== 401) {
    return response;
  }
  const detail = await args.readDetail(response);
  if (!isExpiredReviewDetail(detail)) {
    return response;
  }
  const latest = readReviewSession();
  if (!latest) {
    return response;
  }
  const rotated = await refreshReviewSession(latest, args.refresh);
  return args.perform(rotated.accessToken);
}
