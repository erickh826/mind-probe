// Runtime endpoints for the Wizard console.
//
// Default is the *same origin* as the Vite dev server: `vite.config.ts` proxies
// `/sessions`, `/cases`, `/health` and `/ws` to the Session Server. Going
// through the proxy keeps the role-bound `mindprobe_session_token` cookie
// (ADR-0001 §8) first-party, which is what makes it ride along on the
// WebSocket upgrade — a WebSocket handshake is not covered by CORS, so a
// genuinely cross-site upgrade would reach the server with no cookie at all.
//
// Set `VITE_API_BASE_URL` (e.g. "http://192.168.1.20:8000") to talk to a
// server directly, e.g. from a `vite preview` / static build where no dev
// proxy exists.

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
