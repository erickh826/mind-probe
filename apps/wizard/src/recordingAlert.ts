// Banner text for a student recording failure.
//
// The live `recording_failed` event and the session snapshot carry the same
// notice. A Wizard that joins late or refreshes restores the banner from the
// snapshot; null clears it.

export interface RecordingFailureNotice {
  phase?: string | null;
  step?: string | null;
  error_name?: string | null;
  message?: string | null;
}

export function recordingAlertFromNotice(
  notice: RecordingFailureNotice | null | undefined,
): string | null {
  if (notice == null) {
    return null;
  }
  const message =
    typeof notice.message === "string" && notice.message.trim().length > 0
      ? notice.message.trim()
      : "";
  const errorName = typeof notice.error_name === "string" ? notice.error_name.trim() : "";
  const step = typeof notice.step === "string" ? notice.step.trim() : "";
  if (message.length === 0 && errorName.length === 0 && step.length === 0) {
    return null;
  }
  const text = message.length > 0 ? message : "學生端錄影未能開始或無法繼續寫入。";
  const detail = [errorName, step].filter((part) => part.length > 0).join(" · ");
  return detail.length > 0 ? `${text}（${detail}）` : text;
}
