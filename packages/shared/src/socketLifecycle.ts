// One live socket per role, as the Student and the Wizard see it.
//
// The server refuses an upgrade by accepting it and closing at once, so a
// browser fires `onopen` even for a refused socket. An open transport is
// therefore not admission: only the first `session_snapshot` on the current
// socket is (the server answers `reconnect_hello` only on an admitted socket).
// Until then the status stays connecting/reconnecting, the backoff keeps
// growing, and nothing may start a recording.
//
// Pure bookkeeping, no timers or sockets: the hooks own those and apply the
// returned effects. Every call names its socket, and a socket that is no
// longer current is ignored, so a late callback from a replaced or detached
// socket cannot admit, close, or schedule anything.

export const SOCKET_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000] as const;

/** How long an open transport may wait for its snapshot before it is dropped. */
export const ADMISSION_TIMEOUT_MS = 8_000;

/**
 * Why a socket ended, from its close code and reason.
 *
 * - `occupied`: 4429 role_already_connected. Another device holds the role
 *   and heartbeated in the last 30 s; retry.
 * - `unavailable`: 1013, a transient server failure; retry.
 * - `replaced`: 4409 role_connection_replaced. Another device took the role
 *   over; final.
 * - `revoked`: 4401 token_revoked; final, and the credential is dead.
 * - `ended`: 4401 session_ended; final, uploads still have their grace.
 * - `refused`: any other 4400/4401/4403/4404 refusal; final.
 * - `lost`: anything else (network drop, 1006, timeouts); retry.
 */
export type CloseKind =
  | "occupied"
  | "unavailable"
  | "replaced"
  | "revoked"
  | "ended"
  | "refused"
  | "lost";

const REFUSAL_CODES = new Set([4400, 4401, 4403, 4404]);

export function classifyClose(code: number, reason: string): CloseKind {
  if (code === 4429) {
    return "occupied";
  }
  if (code === 1013) {
    return "unavailable";
  }
  if (code === 4409) {
    // An older server also closed a refused newcomer 4409, but before
    // accept(), so a browser never saw that one. 4409 now only means replaced.
    return "replaced";
  }
  if (code === 4401 && reason === "token_revoked") {
    return "revoked";
  }
  if (code === 4401 && reason === "session_ended") {
    return "ended";
  }
  if (REFUSAL_CODES.has(code)) {
    return "refused";
  }
  return "lost";
}

export function isRetryableClose(kind: CloseKind): boolean {
  return kind === "occupied" || kind === "unavailable" || kind === "lost";
}

export type LifecycleStatus = "connecting" | "reconnecting" | "open" | "closed";

/** What the hook should do. A field that is absent leaves that state alone. */
export interface SocketEffects {
  status?: LifecycleStatus;
  /** Reason of a final close, for the UI and the revocation latch. */
  closedReason?: string | null;
  /** Why the socket is not admitted right now (`occupied`, `unavailable`, ...). */
  notice?: CloseKind | null;
  /** Send `reconnect_hello` on the current socket. */
  sendHello?: boolean;
  /** This socket was just admitted: start heartbeat and clock sync, replay. */
  admitted?: boolean;
  /** Detach the socket's handlers and close it; its `onclose` will be stale. */
  detach?: boolean;
  /** Connect again after this many ms. */
  reconnectInMs?: number;
  /** No more reconnects. */
  stop?: boolean;
}

export class SocketLifecycle {
  private current: number | null = null;
  private nextId = 1;
  private attempt = 0;
  private admittedId: number | null = null;
  private everAdmitted = false;
  private stopped = false;

  /** A new socket is being opened. Returns its id and the status to show. */
  begin(): { id: number; status: LifecycleStatus } {
    const id = this.nextId;
    this.nextId += 1;
    this.current = id;
    this.admittedId = null;
    return { id, status: this.everAdmitted ? "reconnecting" : "connecting" };
  }

  isCurrent(id: number): boolean {
    return !this.stopped && this.current === id;
  }

  isAdmitted(id: number): boolean {
    return this.isCurrent(id) && this.admittedId === id;
  }

  /** Transport open. Not admission: ask for the snapshot and wait. */
  opened(id: number): SocketEffects {
    if (!this.isCurrent(id)) {
      return {};
    }
    return { sendHello: true };
  }

  /** A `session_snapshot` arrived on this socket. The first one admits it. */
  snapshot(id: number): SocketEffects {
    if (!this.isCurrent(id) || this.admittedId === id) {
      return {};
    }
    this.admittedId = id;
    this.everAdmitted = true;
    this.attempt = 0;
    return { status: "open", closedReason: null, notice: null, admitted: true };
  }

  /** The socket closed. Final refusals stop; the rest back off and retry. */
  closed(id: number, code: number, reason: string): SocketEffects {
    if (!this.isCurrent(id)) {
      return {};
    }
    this.current = null;
    this.admittedId = null;
    const kind = classifyClose(code, reason);
    if (!isRetryableClose(kind)) {
      this.stopped = true;
      return {
        status: "closed",
        closedReason: reason || String(code),
        notice: kind,
        stop: true,
      };
    }
    return this.retry(kind);
  }

  /**
   * The current socket stopped answering (missed heartbeats) or was never
   * admitted in time. Drop it now rather than waiting for a close event a
   * silent network may take a long time to deliver.
   */
  timedOut(id: number): SocketEffects {
    if (!this.isCurrent(id)) {
      return {};
    }
    this.current = null;
    this.admittedId = null;
    return { detach: true, ...this.retry("lost") };
  }

  /** The hook is going away. Nothing current, nothing retried. */
  dispose(): void {
    this.stopped = true;
    this.current = null;
    this.admittedId = null;
  }

  private retry(kind: CloseKind): SocketEffects {
    const delay = SOCKET_BACKOFF_MS[Math.min(this.attempt, SOCKET_BACKOFF_MS.length - 1)];
    this.attempt += 1;
    return {
      status: "reconnecting",
      notice: kind,
      reconnectInMs: delay,
    };
  }
}
