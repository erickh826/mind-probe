import { useEffect, useState } from "react";

import { OpApiError, type OpClient } from "../api";
import {
  describeProgress,
  errorMessage,
  formatSeconds,
  formatServerTime,
  graceRemainingSeconds,
  labelFor,
  RECORDING_LABELS,
  STATUS_LABELS,
  type ProgressRead,
} from "../opLogic";
import type { OperatorSessionRow, RecordingStatus, ReviewCodesResponse, UploadReissueResponse } from "../types";

interface SessionDetailProps {
  client: OpClient;
  /** Take before a request; false afterwards means the operator signed out meanwhile. */
  authTicket: () => () => boolean;
  row: OperatorSessionRow;
  nowMs: number;
  progress: { previous?: ProgressRead; current: ProgressRead } | undefined;
  onUploadCode: (sessionId: string, issued: UploadReissueResponse) => void;
  onReviewCodes: (sessionId: string, codes: ReviewCodesResponse) => void;
}

type Confirming = "upload" | "review" | null;

function describeError(error: unknown): string {
  if (error instanceof OpApiError) {
    return errorMessage(error.status, error.detail);
  }
  return errorMessage(0, "");
}

const MISSING_PREVIEW = 20;

export function SessionDetail({
  client,
  authTicket,
  row,
  nowMs,
  progress,
  onUploadCode,
  onReviewCodes,
}: SessionDetailProps) {
  const sessionId = row.session_id;
  const [status, setStatus] = useState<RecordingStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<Confirming>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // The list refresh is the operator's "read again"; follow it here too. A newer
  // read cancels an older one still in flight, so the older cannot land last.
  const readAtMs = progress?.current.readAtMs;
  useEffect(() => {
    let cancelled = false;
    setStatusError(null);
    client.recordingStatus(sessionId).then(
      (next) => {
        if (!cancelled) {
          setStatus(next);
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setStatus(null);
          setStatusError(describeError(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, sessionId, readAtMs]);

  // The parent keys this component by session, so a code that arrives after the
  // operator switched away is still filed under the session it was issued for.
  async function issueUploadCode() {
    const live = authTicket();
    setActionBusy(true);
    setActionError(null);
    try {
      const issued = await client.reissueUploadCode(sessionId);
      if (!live()) {
        return;
      }
      onUploadCode(sessionId, issued);
      setConfirming(null);
    } catch (error) {
      if (live()) {
        setActionError(describeError(error));
      }
    } finally {
      setActionBusy(false);
    }
  }

  async function regenerateReviewCodes() {
    const live = authTicket();
    setActionBusy(true);
    setActionError(null);
    try {
      const codes = await client.regenerateReviewCodes(sessionId);
      if (!live()) {
        return;
      }
      onReviewCodes(sessionId, codes);
      setConfirming(null);
    } catch (error) {
      if (live()) {
        setActionError(describeError(error));
      }
    } finally {
      setActionBusy(false);
    }
  }

  const grace = graceRemainingSeconds(row.upload_grace_ends_at, nowMs);
  const missing = status?.missing_sequences ?? [];

  return (
    <section className="panel detail">
      <h3>錄影狀態 · {sessionId}</h3>
      <dl className="facts">
        <dt>場次狀態</dt>
        <dd>
          {labelFor(STATUS_LABELS, row.status)}
          {row.pause_reason ? `（${row.pause_reason}）` : ""}
        </dd>
        <dt>結束時間</dt>
        <dd>{formatServerTime(row.ended_at)}</dd>
        <dt>補傳寬限剩餘</dt>
        <dd>{grace === null ? "場次未結束，寬限期尚未開始" : formatSeconds(grace)}</dd>
        <dt>錄影 / finalize</dt>
        <dd>
          {labelFor(RECORDING_LABELS, row.recording_state)} / {row.finalize_state ?? "—"}
          {row.incomplete_reason ? `（${row.incomplete_reason}）` : ""}
        </dd>
        <dt>已存片段</dt>
        <dd>
          {row.stored_sequences}，距上一片段 {formatSeconds(row.seconds_since_last_chunk)}
        </dd>
        <dt>兩次讀取比較</dt>
        <dd>{progress ? describeProgress(progress.previous, progress.current) : "—"}</dd>
      </dl>

      {statusError && <p className="error">錄影明細：{statusError}</p>}
      {status && (
        <dl className="facts">
          <dt>片段範圍</dt>
          <dd>
            {status.first_sequence ?? "—"} – {status.last_sequence ?? "—"}，final_sequence{" "}
            {status.final_sequence ?? "未提交"}
          </dd>
          <dt>缺漏片段</dt>
          <dd>
            {missing.length === 0
              ? "無"
              : `${missing.length} 個：${missing.slice(0, MISSING_PREVIEW).join(", ")}${
                  missing.length > MISSING_PREVIEW ? " …" : ""
                }`}
          </dd>
          <dt>Header 片段</dt>
          <dd>{status.chunk_count === 0 ? "尚無片段" : status.header_chunk_ok ? "正常" : "缺少或無效"}</dd>
          <dt>原始 WebM / MP4</dt>
          <dd>
            {status.raw_webm.exists ? "有" : "無"} / {status.mp4.exists ? "有" : "無"}
          </dd>
        </dl>
      )}

      <div className="actions">
        <h4>補傳碼</h4>
        {row.upload_reissue_refusal ? (
          <p className="hint">目前不能發碼：{errorMessage(409, row.upload_reissue_refusal)}</p>
        ) : confirming === "upload" ? (
          <div className="confirm">
            <p>
              發新碼會讓這個場次先前未使用的補傳碼失效。只在學生仍使用保存原資料的瀏覽器、且確認需要補傳時發碼。
            </p>
            <button type="button" disabled={actionBusy} onClick={issueUploadCode}>
              {actionBusy ? "發碼中…" : "確定發碼"}
            </button>
            <button type="button" className="secondary" disabled={actionBusy} onClick={() => setConfirming(null)}>
              取消
            </button>
          </div>
        ) : (
          <button type="button" onClick={() => setConfirming("upload")}>
            發補傳碼
          </button>
        )}

        <h4>Review code</h4>
        {confirming === "review" ? (
          <div className="confirm">
            <p>重新產生後，舊的老師與觀察員 review code 立即失效；已登入的審閱不受影響。</p>
            <button type="button" disabled={actionBusy} onClick={regenerateReviewCodes}>
              {actionBusy ? "產生中…" : "確定重新產生"}
            </button>
            <button type="button" className="secondary" disabled={actionBusy} onClick={() => setConfirming(null)}>
              取消
            </button>
          </div>
        ) : (
          <button type="button" onClick={() => setConfirming("review")}>
            重新產生 review code
          </button>
        )}
        {actionError && <p className="error">{actionError}</p>}
      </div>
    </section>
  );
}
