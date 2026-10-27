// Review-grant credentials for the teacher app (spec revision 11, ADR-0005 §6.5).
// Access and refresh tokens are not cookies and are not the student upload
// credential.
//
// The refresh token stays in this document's memory. sessionStorage keeps the
// access token only, so Chrome's "Duplicate tab" (which copies sessionStorage
// and not the JS heap) cannot present the same refresh token. A same-tab reload
// parks the refresh token on `pagehide` and the next document claims it only
// when the navigation type is `reload`. Any other navigation discards a parked
// refresh token instead of rotating the chain.

import type { ReviewRole, ReviewTokenResponse } from "./api";
import { clearJoinedSession } from "./sessionAuth.ts";

export interface StoredReviewSession {
  sessionId: string;
  role: ReviewRole;
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
}

const REVIEW_SESSION_KEY = "mindprobe.review_session";
const RELOAD_MARKER_KEY = "mindprobe.review_reload";

let memory: StoredReviewSession | null = null;
const knownAccessTokens = new Set<string>();
let refreshFlight: Promise<StoredReviewSession> | null = null;
let sessionEpoch = 0;
let guardsInstalled = false;
let navigationTypeOverride: string | null = null;

type ReviewDropListener = (detail: string) => void;
const dropListeners = new Set<ReviewDropListener>();

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
    typeof record.expiresAt === "number" &&
    Number.isFinite(record.expiresAt)
  );
}

function hold(session: StoredReviewSession): StoredReviewSession {
  memory = session;
  knownAccessTokens.add(session.accessToken);
  return session;
}

function persistReview(session: StoredReviewSession, includeRefresh: boolean): void {
  const payload: StoredReviewSession = includeRefresh
    ? session
    : { ...session, refreshToken: "" };
  writeStorage(REVIEW_SESSION_KEY, JSON.stringify(payload));
}

function navigationType(): string {
  if (navigationTypeOverride !== null) {
    return navigationTypeOverride;
  }
  if (typeof performance === "undefined" || typeof performance.getEntriesByType !== "function") {
    return "";
  }
  const entry = performance.getEntriesByType("navigation")[0];
  if (!entry || !("type" in entry)) {
    return "";
  }
  const type = entry.type;
  return typeof type === "string" ? type : "";
}

/**
 * A reload of this tab may take the refresh token that `pagehide` parked.
 * Duplicate-tab navigations are `back_forward` or `navigate`, so they must not.
 * An empty type (tests, or a browser without the navigation entry) falls back
 * to the one-shot marker, which a copied sessionStorage would also contain —
 * real browsers do not use that fallback.
 */
function shouldClaimParkedRefresh(): boolean {
  const type = navigationType();
  if (type === "reload") {
    return true;
  }
  if (type === "navigate" || type === "back_forward" || type === "prerender") {
    return false;
  }
  return readStorage(RELOAD_MARKER_KEY) === "1";
}

/** Keep the review login in memory. sessionStorage does not receive the refresh token. */
export function rememberReviewSession(session: StoredReviewSession): StoredReviewSession {
  const stored = hold(session);
  persistReview(stored, false);
  removeStorage(RELOAD_MARKER_KEY);
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
  let parsed: StoredReviewSession;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isStoredReviewSession(value)) {
      return null;
    }
    parsed = value;
  } catch {
    return null;
  }
  const claimRefresh = parsed.refreshToken.length > 0 && shouldClaimParkedRefresh();
  const session: StoredReviewSession = claimRefresh
    ? parsed
    : { ...parsed, refreshToken: "" };
  const held = hold(session);
  persistReview(held, false);
  removeStorage(RELOAD_MARKER_KEY);
  return held;
}

/** Drop the review login so the next request cannot refresh it. */
export function clearReviewSession(): void {
  sessionEpoch += 1;
  memory = null;
  knownAccessTokens.clear();
  refreshFlight = null;
  removeStorage(REVIEW_SESSION_KEY);
  removeStorage(RELOAD_MARKER_KEY);
  clearJoinedSession();
}

function detailOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "invalid_refresh_token";
}

function notifyReviewDrop(detail: string): void {
  for (const listener of dropListeners) {
    listener(detail);
  }
}

/**
 * Clear stored review credentials and tell the teacher UI to return to review login.
 * Listeners run only when this document actually held a review session.
 */
export function dropUnrecoverableReviewSession(detail: string): void {
  const hadCredentials = memory !== null || readStorage(REVIEW_SESSION_KEY) !== null;
  clearReviewSession();
  if (hadCredentials) {
    notifyReviewDrop(detail);
  }
}

/** Fired when a stored review session is discarded because it cannot be refreshed. */
export function subscribeReviewSessionDrop(listener: ReviewDropListener): () => void {
  dropListeners.add(listener);
  return () => {
    dropListeners.delete(listener);
  };
}

/** Write the refresh token back for a same-tab reload. Duplicate tabs must not claim it. */
export function parkReviewRefreshForReload(): void {
  if (!memory?.refreshToken) {
    return;
  }
  persistReview(memory, true);
  writeStorage(RELOAD_MARKER_KEY, "1");
}

/** A bfcache restore kept this document's memory, so take the refresh token back out of storage. */
export function unparkReviewRefresh(): void {
  removeStorage(RELOAD_MARKER_KEY);
  if (memory) {
    persistReview(memory, false);
  }
}

/** Install the reload handoff. Safe to call more than once. */
export function installReviewTabGuards(): void {
  if (guardsInstalled || typeof window === "undefined") {
    return;
  }
  guardsInstalled = true;
  window.addEventListener("pagehide", () => {
    parkReviewRefreshForReload();
  });
  window.addEventListener("pageshow", (event: PageTransitionEvent) => {
    if (event.persisted) {
      unparkReviewRefresh();
    }
  });
}

/** Test hook: drop every in-memory review credential, including a refresh in flight. */
export function resetReviewSessionStateForTests(): void {
  clearReviewSession();
  refreshFlight = null;
  navigationTypeOverride = null;
  dropListeners.clear();
}

/** Test hook: force the next read to come from sessionStorage. */
export function forgetReviewMemoryForTests(): void {
  memory = null;
}

/** Test hook: `""` means the navigation type is unknown. `null` reads `performance`. */
export function setReviewNavigationTypeForTests(type: string | null): void {
  navigationTypeOverride = type;
}

export function isExpiredReviewDetail(detail: string): boolean {
  return detail === "expired_session_token" || detail === "expired";
}

export function isUnrecoverableReviewDetail(detail: string): boolean {
  return (
    detail === "revoked_session_token" ||
    detail === "refresh_reused" ||
    detail === "invalid_refresh_token"
  );
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
  if (!current?.refreshToken) {
    return Promise.reject(new Error("review session missing"));
  }
  if (current.refreshToken !== expected.refreshToken) {
    return Promise.resolve(current);
  }
  if (refreshFlight) {
    return refreshFlight;
  }
  const flight = (async () => {
    const epoch = sessionEpoch;
    try {
      const latest = readReviewSession();
      if (!latest?.refreshToken) {
        throw new Error("review session missing");
      }
      const body = await refresh(latest.refreshToken);
      if (sessionEpoch !== epoch) {
        throw new Error("review session missing");
      }
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
      if (sessionEpoch === epoch) {
        dropUnrecoverableReviewSession(detailOf(error));
      }
      throw error;
    }
  })();
  refreshFlight = flight;
  void flight.then(
    () => {
      if (refreshFlight === flight) {
        refreshFlight = null;
      }
    },
    () => {
      if (refreshFlight === flight) {
        refreshFlight = null;
      }
    },
  );
  return flight;
}

/**
 * Call `perform` with the latest review access token. A 401 `expired_session_token`
 * refreshes once (single-flight) and retries when this document holds a refresh
 * token. Revoked and other unrecoverable 401s clear stored credentials and are
 * returned as-is. `sessionId` skips refresh when the path names a different session.
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
    if (isUnrecoverableReviewDetail(detail)) {
      dropUnrecoverableReviewSession(detail);
    }
    return response;
  }
  const latest = readReviewSession();
  if (!latest?.refreshToken) {
    dropUnrecoverableReviewSession(detail);
    return response;
  }
  const rotated = await refreshReviewSession(latest, args.refresh);
  return args.perform(rotated.accessToken);
}
