// When the Student page may offer "reload for the new version".
//
// A reload stops MediaRecorder and drops in-flight uploads, so the prompt
// stays hidden while anything is recording, queued or awaiting confirmation.
// Allowed: before joining, before any recording in this tab, or once the
// close banner says the recording is safely complete.

import type { RecorderStatus } from "./types";
import type { CloseBanner } from "./uploadQueue";

export interface StudentUpdateGate {
  joined: boolean;
  recorderStatus: RecorderStatus;
  pendingCount: number;
  bannerKind: CloseBanner["kind"];
}

const RECORDER_BUSY: ReadonlySet<RecorderStatus> = new Set(["requesting", "recording", "stopping"]);

export function canPromptStudentUpdate(gate: StudentUpdateGate): boolean {
  if (RECORDER_BUSY.has(gate.recorderStatus) || gate.pendingCount > 0) {
    return false;
  }
  if (gate.bannerKind === "safe") {
    return true;
  }
  // confirming, uploading, interrupted, incomplete and needs_operator all
  // still have recording work or a decision pending.
  if (gate.bannerKind !== "none") {
    return false;
  }
  // Stopped with no banner yet: finalize has not been queued. Wait for "safe".
  return !gate.joined || gate.recorderStatus !== "stopped";
}
