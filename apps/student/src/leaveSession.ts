// Leave the student console without deleting recorded chunks.
//
// Three different actions:
// - Stop REC stops the recorder and stays on this page.
// - End session is the Wizard's control; this page does not send it.
// - Leave clears this device's saved join info and returns to the join page.
//
// A new-version prompt must not force-reload while recording or re-uploading.
// Leave never reloads the page. IndexedDB is left untouched.

import type { CloseBanner } from "./uploadQueue.ts";

export type StudentLeaveKind = "recording" | "pending_upload" | "complete" | "incomplete" | "idle";

export interface StudentLeavePlan {
  kind: StudentLeaveKind;
  /** Saved join/login for this session is dropped so a refresh cannot re-enter it. */
  clearSavedJoin: true;
  /** Never delete IndexedDB chunks or site data. */
  deleteLocalRecording: false;
  /** Never force a reload. */
  reload: false;
  buttonLabel: string;
  title: string;
  detail: string;
}

export function planStudentLeave(input: {
  banner: CloseBanner;
  recorderStatus: string;
  pendingCount: number;
}): StudentLeavePlan {
  const base = {
    clearSavedJoin: true as const,
    deleteLocalRecording: false as const,
    reload: false as const,
  };
  if (
    input.recorderStatus === "recording" ||
    input.recorderStatus === "requesting" ||
    input.recorderStatus === "stopping"
  ) {
    return {
      ...base,
      kind: "recording",
      buttonLabel: "離開場次",
      title: "離開場次（錄影中）",
      detail:
        "這會停止錄影、關閉攝影機與連線，並回到加入頁。已保存的片段會留在這台裝置，不會被刪除，並標為不完整。這不是 Stop REC（Stop REC 會留在此頁），也不是結束場次（結束場次由 Wizard 操作）。",
    };
  }
  if (input.banner.kind === "safe") {
    return {
      ...base,
      kind: "complete",
      buttonLabel: "回到加入頁",
      title: "可以安全關閉",
      detail:
        "伺服器已確認錄影完整，可以安全關閉。回到加入頁會清除這台裝置保存的加入資料，並關閉連線與攝影機。重新整理不會回到這場。",
    };
  }
  if (
    input.banner.kind === "uploading" ||
    input.banner.kind === "confirming" ||
    input.pendingCount > 0
  ) {
    return {
      ...base,
      kind: "pending_upload",
      buttonLabel: "離開場次",
      title: "尚有片段未上傳",
      detail:
        "上傳進度會留在本機佇列。離開不會刪除已保存的片段，加入頁仍有補傳入口。這台裝置的加入資料會清除，重新整理不會回到這場。",
    };
  }
  if (
    input.banner.kind === "interrupted" ||
    input.banner.kind === "incomplete" ||
    input.banner.kind === "needs_operator"
  ) {
    return {
      ...base,
      kind: "incomplete",
      buttonLabel: "離開場次",
      title: "錄影不完整",
      detail:
        "錄影不完整，狀態會留在畫面上。可以回到加入頁。本機尚未上傳的片段會保留，不會被清除，之後仍可補傳。",
    };
  }
  return {
    ...base,
    kind: "idle",
    buttonLabel: "離開場次",
    title: "離開場次",
    detail:
      "清除這台裝置的加入資料，關閉連線與攝影機，回到加入頁。不會結束場次，也不會刪除已保存的錄影。",
  };
}
