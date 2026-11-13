// When the student recorder may start.
//
// The server session state is the gate. Seeing the Wizard online, or the
// student socket being admitted, is not. `active` is the in-progress state
// (ADR-0003's scheduled start has taken effect). `starting` is only the lead
// before that instant. A reconnect does not start another take: a recorder
// that is already running, or a take already stored for this session, continues.

import type { SessionState } from "@mind-probe/shared";

export function serverSessionInProgress(state: SessionState): boolean {
  return state === "active";
}

export function waitingForWizardCopy(state: SessionState, recording: boolean): string | null {
  if (recording) {
    return null;
  }
  switch (state) {
    case "created":
      return "等待 Wizard 開始場次。請先確認攝影機與麥克風預覽。伺服器進入「進行中」之後才會開始錄影；Wizard 在線本身不會開始。";
    case "starting":
      return "Wizard 已按下開始場次，正在等待伺服器的統一開始時間。錄影尚未開始。";
    case "paused":
      return "場次已暫停。新的錄影要等伺服器恢復為進行中才會開始；進行中的錄影不會因此另開一段。";
    default:
      return null;
  }
}

export function shouldAutoStartRecording(input: {
  restoredFromStorage: boolean;
  uploadOnly: boolean;
  admitted: boolean;
  sessionState: SessionState;
  takeCheck: "unknown" | "free" | "claimed";
  canStart: boolean;
  recorderStatus: string;
  alreadyAttempted: boolean;
}): boolean {
  if (input.restoredFromStorage || input.uploadOnly || input.alreadyAttempted) {
    return false;
  }
  if (!input.admitted || !serverSessionInProgress(input.sessionState)) {
    return false;
  }
  if (input.takeCheck !== "free" || !input.canStart) {
    return false;
  }
  return !["recording", "requesting", "stopping", "stopped"].includes(input.recorderStatus);
}

/** A live recorder, or a take already on disk, must not be replaced by a new segment. */
export function reconnectContinuesExistingTake(input: {
  recorderStatus: string;
  hasLocalTake: boolean;
}): boolean {
  return (
    input.hasLocalTake ||
    input.recorderStatus === "recording" ||
    input.recorderStatus === "stopping"
  );
}
