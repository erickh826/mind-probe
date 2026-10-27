// Runtime endpoints for the Wizard console.
//
// Default is the *same origin* as the Vite dev server: `vite.config.ts` proxies
// `/sessions`, `/cases`, `/health` and `/ws` to the Session Server. Production
// builds set `VITE_API_BASE_URL` to the API host. Auth is a Bearer token from
// `/join`, sent on REST and as the WebSocket `bearer.<token>` subprotocol
// (ADR-0005). The token is not placed in the query string.

/** Origin of the Session Server REST API. Empty string = same origin. */
export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL ?? ""
).replace(/\/$/, "");

/** Absolute (or same-origin relative) URL for a REST path. */
export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

/**
 * WebSocket URL for `/ws/{session_id}/wizard` (ADR-0001 §1).
 *
 * Derives ws:// vs wss:// from whichever origin the REST API lives on, so a
 * TLS-terminated deployment upgrades securely without extra configuration.
 */
export function sessionSocketUrl(sessionId: string): string {
  const origin = API_BASE_URL || window.location.origin;
  const wsOrigin = origin.replace(/^http/, "ws");
  return `${wsOrigin}/ws/${encodeURIComponent(sessionId)}/wizard`;
}

/** This console always connects as the Wizard role. */
export const WIZARD_ROLE = "wizard" as const;
