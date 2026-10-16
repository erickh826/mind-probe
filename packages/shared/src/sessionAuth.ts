// Browser session credentials for split-origin deploys (ADR-0005 §1).
// The API host is not the Vite/Vercel origin, so the token travels as
// Authorization: Bearer and as a WebSocket subprotocol, not as a cookie.
//
// A reload drops in-memory state. The joined session id, role, and token are
// also kept in sessionStorage so the student and wizard consoles can restore
// a live credential without asking for the one-time join code again.

export const MINDPROBE_WS_PROTOCOL = "mindprobe.v1";

const STORAGE_PREFIX = "mindprobe.access_token.";
const ACTIVE_SESSION_KEY = "mindprobe.active_session_id";
const JOINED_SESSION_KEY = "mindprobe.joined_session";

const memoryTokens = new Map<string, string>();
let activeSessionId: string | null = null;

export interface StoredJoinedSession {
  sessionId: string;
  role: string;
  accessToken: string;
}

function storageKey(sessionId: string): string {
  return `${STORAGE_PREFIX}${sessionId}`;
}

function writeStorage(key: string, value: string): void {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // Private-mode storage can reject the write. The in-memory copy still
    // authorizes this document.
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

function rememberActiveSessionId(sessionId: string): void {
  activeSessionId = sessionId;
  writeStorage(ACTIVE_SESSION_KEY, sessionId);
}

function readActiveSessionId(): string | null {
  if (activeSessionId !== null) {
    return activeSessionId;
  }
  const stored = readStorage(ACTIVE_SESSION_KEY);
  if (stored) {
    activeSessionId = stored;
  }
  return stored;
}

/** Keep the join/login token in memory and sessionStorage. */
export function rememberAccessToken(sessionId: string, token: string): void {
  memoryTokens.set(sessionId, token);
  rememberActiveSessionId(sessionId);
  writeStorage(storageKey(sessionId), token);
}

export function readAccessToken(sessionId: string): string | null {
  const cached = memoryTokens.get(sessionId);
  if (cached) {
    return cached;
  }
  const stored = readStorage(storageKey(sessionId));
  if (stored) {
    memoryTokens.set(sessionId, stored);
  }
  return stored;
}

/** Token remembered most recently in this document, including after reload. */
export function currentAccessToken(): string | null {
  const sessionId = readActiveSessionId();
  if (sessionId === null) {
    return null;
  }
  return readAccessToken(sessionId);
}

/** Persist the joined console so a reload can validate it with the server. */
export function rememberJoinedSession(session: StoredJoinedSession): void {
  rememberAccessToken(session.sessionId, session.accessToken);
  writeStorage(JOINED_SESSION_KEY, JSON.stringify(session));
}

export function readJoinedSession(): StoredJoinedSession | null {
  const raw = readStorage(JOINED_SESSION_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) {
      return null;
    }
    const record = parsed as Partial<StoredJoinedSession>;
    if (
      typeof record.sessionId !== "string" ||
      record.sessionId.length === 0 ||
      typeof record.role !== "string" ||
      record.role.length === 0 ||
      typeof record.accessToken !== "string" ||
      record.accessToken.length === 0
    ) {
      return null;
    }
    const session: StoredJoinedSession = {
      sessionId: record.sessionId,
      role: record.role,
      accessToken: record.accessToken,
    };
    rememberAccessToken(session.sessionId, session.accessToken);
    return session;
  } catch {
    return null;
  }
}

/** Drop the restored join so the console asks for a code again. */
export function clearJoinedSession(sessionId?: string): void {
  const id = sessionId ?? readActiveSessionId();
  activeSessionId = null;
  if (id) {
    memoryTokens.delete(id);
    removeStorage(storageKey(id));
  }
  removeStorage(ACTIVE_SESSION_KEY);
  removeStorage(JOINED_SESSION_KEY);
}

/**
 * Subprotocols for `new WebSocket(url, websocketSubprotocols(token))`.
 * The server echoes only `mindprobe.v1` and never `bearer.*`.
 */
export function websocketSubprotocols(token: string): string[] {
  return [MINDPROBE_WS_PROTOCOL, `bearer.${token}`];
}
