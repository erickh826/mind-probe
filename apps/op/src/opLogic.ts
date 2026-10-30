// Pure helpers for the OP page. No DOM, no fetch, so node --test can run them.

import type { JoinCodeRole } from "./types";

/** Mirrors `recording.SESSION_ID_RE`; ids outside it cannot hold a recording. */
const SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

const ERROR_COPY: Record<string, string> = {
  invalid_operator_token: "Operator token 不正確，或伺服器沒有設定 MINDPROBE_OPERATOR_TOKEN。",
  disk_full: "伺服器磁碟空間不足，已拒絕建立場次。",
  rate_limited: "操作太頻繁，請稍候一分鐘再試。",
  upload_security_revoked: "此場次的上傳權限已因安全原因撤銷，不能發補傳碼。",
  upload_grace_expired: "場次結束後的補傳寬限期已過，不能再發補傳碼。保留資料並另行處理。",
  upload_reissue_refused: "目前的場次狀態不能發補傳碼。",
  recording_deleted: "此場次的錄影已刪除。",
  session_ended: "場次已結束，不能再換 join code（結束後也不能用 join code 入場）。",
};

export function errorMessage(status: number, detail: string): string {
  if (ERROR_COPY[detail]) {
    return ERROR_COPY[detail];
  }
  if (detail.startsWith("session already exists:")) {
    return "這個場次 ID 已經存在，請換一個。";
  }
  if (detail.startsWith("session not found:")) {
    return "找不到這個場次。";
  }
  if (status === 0) {
    return "連不上伺服器。檢查網路或 API 位址。";
  }
  return detail ? `${detail}（HTTP ${status}）` : `HTTP ${status}`;
}

/** `TRIAL-YYYYMMDD-HHMMSS` in local time, the naming used for trial runs. */
export function defaultSessionId(now: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
  const time = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `TRIAL-${date}-${time}`;
}

/** Why a session id would be rejected for recording storage, or null. */
export function sessionIdProblem(sessionId: string): string | null {
  if (!sessionId) {
    return "請輸入場次 ID。";
  }
  if (!SESSION_ID_RE.test(sessionId)) {
    return "場次 ID 只能用英數字、底線、連字號，開頭須為英數字，最長 128 字。";
  }
  return null;
}

/** Seconds left in the post-end upload grace, 0 once it has passed, null if not ended. */
export function graceRemainingSeconds(graceEndsAt: string | null, nowMs: number): number | null {
  if (!graceEndsAt) {
    return null;
  }
  const endMs = Date.parse(graceEndsAt);
  if (Number.isNaN(endMs)) {
    return null;
  }
  return Math.max(0, Math.floor((endMs - nowMs) / 1000));
}

export function formatSeconds(seconds: number | null): string {
  if (seconds === null) {
    return "—";
  }
  if (seconds < 60) {
    return `${seconds} 秒`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) {
    return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours} 時 ${minutes % 60} 分`;
}

/** Parse a server timestamp. Naive values are UTC (``models._utcnow``). */
export function parseServerTime(value: string | null): Date | null {
  if (!value) {
    return null;
  }
  const hasOffset = /(?:[zZ]|[+-]\d\d:\d\d)$/.test(value);
  const parsed = new Date(hasOffset ? value : `${value}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatServerTime(value: string | null): string {
  const parsed = parseServerTime(value);
  if (!parsed) {
    return value ?? "—";
  }
  return parsed.toLocaleString();
}

export interface ProgressRead {
  storedSequences: number;
  readAtMs: number;
}

/**
 * Compare two reads of the same session, as docs/recording-operations.md asks.
 * A single `seconds_since_last_chunk` cannot show that a recorder is lost.
 */
export function describeProgress(previous: ProgressRead | undefined, current: ProgressRead): string {
  if (!previous || previous.readAtMs >= current.readAtMs) {
    return "第一次讀取；稍後再重新整理一次才能比較進度。";
  }
  const delta = current.storedSequences - previous.storedSequences;
  const elapsed = Math.round((current.readAtMs - previous.readAtMs) / 1000);
  if (delta > 0) {
    return `${elapsed} 秒內新增 ${delta} 個片段，上傳仍在進行。`;
  }
  if (delta < 0) {
    return `片段數比上次少 ${-delta} 個（可能已清理或刪除），請查看錄影狀態。`;
  }
  return `${elapsed} 秒內沒有新片段。`;
}

export interface ProgressReads {
  [sessionId: string]: { previous?: ProgressRead; current: ProgressRead };
}

interface ReadableRow {
  session_id: string;
  stored_sequences: number;
}

/** A full re-read: every listed row's last read becomes its previous one. */
export function nextReads(previous: ProgressReads, rows: ReadableRow[], readAtMs: number): ProgressReads {
  const reads: ProgressReads = {};
  for (const row of rows) {
    reads[row.session_id] = {
      previous: previous[row.session_id]?.current,
      current: { storedSequences: row.stored_sequences, readAtMs },
    };
  }
  return reads;
}

/** An older page: only rows not read before get a first read; the rest keep theirs. */
export function addFirstReads(reads: ProgressReads, rows: ReadableRow[], readAtMs: number): ProgressReads {
  const merged = { ...reads };
  for (const row of rows) {
    if (!(row.session_id in merged)) {
      merged[row.session_id] = { current: { storedSequences: row.stored_sequences, readAtMs } };
    }
  }
  return merged;
}

/** Append an older page, keeping the rows already shown and dropping repeats. */
export function appendPage<T extends { session_id: string }>(rows: T[], page: T[]): T[] {
  const seen = new Set(rows.map((row) => row.session_id));
  return [...rows, ...page.filter((row) => !seen.has(row.session_id))];
}

/**
 * Invalidates async work that started before the latest `advance()`.
 * Take a ticket before an await; drop the response if the ticket is no longer valid.
 */
export function createGeneration() {
  let current = 0;
  return {
    /** Move on, invalidating every earlier ticket; returns a ticket for the new generation. */
    advance(): () => boolean {
      current += 1;
      const issued = current;
      return () => issued === current;
    },
    /** A check that stays true until the next `advance()`. */
    ticket(): () => boolean {
      const issued = current;
      return () => issued === current;
    },
  };
}

export type Generation = ReturnType<typeof createGeneration>;

/** The confirm-step copy for a join-code reissue, in display order. */
export function joinReissueNotes(roles: JoinCodeRole[], revokeLive: boolean): string[] {
  const notes = ["所選角色的舊 join code，不論用過與否，都會立即失效。"];
  const singleConnection = roles.includes("student") || roles.includes("wizard");
  if (!revokeLive) {
    notes.push("已入場的人不受影響，可以繼續。");
    if (singleConnection) {
      notes.push(
        "學生和 Wizard 各只能有一個連線：舊裝置 30 秒內仍有心跳時，用新碼的新裝置會被拒絕。舊裝置還連著時要換裝置，請勾選「同時中斷舊連線」。",
      );
    }
    return notes;
  }
  notes.push("會立即中斷所選角色目前的連線，舊 token 不能再使用。Review code 和補傳碼不受影響。");
  if (roles.includes("student")) {
    notes.push(
      "撤銷學生連線會中斷錄影。已保存的片段仍可在權限及期限允許時補傳，但錄影可能維持 incomplete；如需重新錄影，請建立新場次。",
    );
  }
  return notes;
}

export interface AttentionInput {
  status: string;
  recording_state: string | null;
  finalize_state: string | null;
  incomplete_reason: string | null;
  needs_operator: boolean;
}

/** Short labels for rows an operator should look at. Empty when nothing stands out. */
export function attentionFlags(row: AttentionInput): string[] {
  const flags: string[] = [];
  if (row.needs_operator) {
    flags.push("需要 OP 處理");
  }
  if (row.incomplete_reason === "recorder_lost") {
    flags.push("錄影中斷");
  } else if (row.recording_state === "incomplete" || row.finalize_state === "incomplete") {
    flags.push("錄影不完整");
  }
  if (row.recording_state === "transcode_failed") {
    flags.push("轉檔失敗");
  }
  if (row.status === "ended" && row.recording_state === null) {
    flags.push("已結束但沒有錄影");
  }
  return flags;
}

export const STATUS_LABELS: Record<string, string> = {
  created: "已建立",
  starting: "開始中",
  active: "進行中",
  paused: "暫停",
  ended: "已結束",
};

export const RECORDING_LABELS: Record<string, string> = {
  recording: "錄影中",
  finalizing: "收尾中",
  complete: "完整",
  incomplete: "不完整",
  transcoding: "轉檔中",
  transcoded: "已轉檔",
  transcode_failed: "轉檔失敗",
  uploaded: "已上傳",
  deleting: "刪除中",
  deleted: "已刪除",
};

export function labelFor(labels: Record<string, string>, value: string | null): string {
  if (value === null) {
    return "—";
  }
  return labels[value] ?? value;
}
