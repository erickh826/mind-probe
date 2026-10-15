// Browser session credentials for split-origin deploys (ADR-0005 §1).
// The API host is not the Vite/Vercel origin, so the token travels as
// Authorization: Bearer and as a WebSocket subprotocol, not as a cookie.

export const MINDPROBE_WS_PROTOCOL = "mindprobe.v1";

const STORAGE_PREFIX = "mindprobe.access_token.";

const memoryTokens = new Map<string, string>();
let activeSessionId: string | null = null;

function storageKey(sessionId: string): string {
  return `${STORAGE_PREFIX}${sessionId}`;
}

/** Keep the join/login token in memory and sessionStorage. */
export function rememberAccessToken(sessionId: string, token: string): void {
  memoryTokens.set(sessionId, token);
  activeSessionId = sessionId;
  try {
    sessionStorage.setItem(storageKey(sessionId), token);
  } catch {
    // Private-mode storage can reject the write. The in-memory copy still
    // authorizes this document.
  }
}

export function readAccessToken(sessionId: string): string | null {
  const cached = memoryTokens.get(sessionId);
  if (cached) {
    return cached;
  }
  try {
    const stored = sessionStorage.getItem(storageKey(sessionId));
    if (stored) {
      memoryTokens.set(sessionId, stored);
    }
    return stored;
  } catch {
    return null;
  }
}

/** Token remembered most recently in this document, if any. */
export function currentAccessToken(): string | null {
  if (activeSessionId === null) {
    return null;
  }
  return readAccessToken(activeSessionId);
}

/**
 * Subprotocols for `new WebSocket(url, websocketSubprotocols(token))`.
 * The server echoes only `mindprobe.v1` and never `bearer.*`.
 */
export function websocketSubprotocols(token: string): string[] {
  return [MINDPROBE_WS_PROTOCOL, `bearer.${token}`];
}
