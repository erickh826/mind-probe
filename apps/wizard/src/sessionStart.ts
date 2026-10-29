// Wizard start control (ADR-0003 §5).
//
// Starting is the existing `session_started` envelope. The server answers with
// `session_start_scheduled` (monotonic now + 2s) and the snapshot stays
// `starting` until that instant. This module only decides when the button may
// send, and when to re-read that snapshot. It does not create a session.

import type { SessionState } from "@mind-probe/shared";

/** Shown when the socket is not open at the moment of the click. */
export const START_OFFLINE_MESSAGE = "連線中斷，開始場次未送出";

const START_LABEL = "開始場次";
const STARTING_LABEL = "開始中…";

export interface StartControlInput {
  connected: boolean;
  sessionState: SessionState;
  /** Local end, snapshot `ended`, or the socket close reason `session_ended`. */
  sessionEnded: boolean;
  /** A `session_started` envelope is waiting for the server. */
  startInFlight: boolean;
  /** Another lifecycle dialog already owns the console. */
  blocked?: boolean;
}

export interface StartControl {
  enabled: boolean;
  pending: boolean;
  label: string;
  title: string;
}

/** True when a start envelope is legal for the lifecycle the console can see. */
export function canDispatchSessionStart(input: {
  sessionState: SessionState;
  sessionEnded: boolean;
  startInFlight: boolean;
}): boolean {
  return (
    !input.sessionEnded &&
    input.sessionState === "created" &&
    !input.startInFlight
  );
}

/**
 * Button state for 開始場次.
 *
 * Enabled only from `created`, while connected, with no start in flight and
 * no other lifecycle dialog open. Every other state stays visible and explains
 * itself, so a second click cannot send another `session_started`.
 */
export function startControl(input: StartControlInput): StartControl {
  if (input.sessionEnded || input.sessionState === "ended") {
    return disabled(START_LABEL, "場次已結束");
  }
  if (input.blocked) {
    return disabled(START_LABEL, "請先完成目前的場次操作");
  }
  if (input.sessionState === "paused") {
    return disabled(START_LABEL, "場次已暫停，請使用恢復");
  }
  if (input.sessionState === "active") {
    return disabled(START_LABEL, "場次進行中");
  }
  if (input.sessionState === "starting") {
    return disabled(START_LABEL, "場次已排定統一開始時間");
  }
  if (input.sessionState !== "created") {
    return disabled(START_LABEL, "目前狀態不能開始場次");
  }
  if (!input.connected) {
    return disabled(START_LABEL, "連線中斷，無法開始場次");
  }
  if (input.startInFlight) {
    return {
      enabled: false,
      pending: true,
      label: STARTING_LABEL,
      title: "正在等待伺服器確認開始",
    };
  }
  return {
    enabled: true,
    pending: false,
    label: START_LABEL,
    title: "開始目前場次",
  };
}

function disabled(label: string, title: string): StartControl {
  return { enabled: false, pending: false, label, title };
}

/** Traditional Chinese message for a rejected `session_started` ack. */
export function startRejectionMessage(reason: string | null): string {
  switch (reason) {
    case "invalid_lifecycle_transition":
      return "無法開始場次：目前狀態不能開始";
    case "session_start_requires_wizard":
      return "無法開始場次：只有 Wizard 可以開始";
    case "session_not_found":
      return "無法開始場次：找不到場次";
    case "persistence_unavailable":
      return "無法開始場次：伺服器暫時無法儲存";
    case "read_only_role_cannot_emit_events":
      return "無法開始場次：此角色不能開始場次";
    default:
      return `無法開始場次：${reason ?? "未知錯誤"}`;
  }
}

/**
 * Delay before asking the server whether a scheduled start is now `active`.
 *
 * `start_at_server_ms` is server monotonic time. With a clock offset, wait
 * until that instant. Without one, wait the protocol lead (2s) and then read
 * the snapshot — a missing offset must not flip the console to active.
 */
export function authoritativeStartCheckDelayMs(
  startAtServerMs: number,
  clientNowMs: number,
  clockOffsetMs: number | null,
  leadMs = 2_000,
): number {
  if (clockOffsetMs === null) {
    return leadMs;
  }
  return Math.max(0, Math.round(startAtServerMs - (clientNowMs + clockOffsetMs)));
}
