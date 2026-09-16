import { useState } from "react";
import { PAUSE_REASONS } from "@mind-probe/shared";
import type { PauseReason } from "@mind-probe/shared";

// 暫停原因選擇（ADR-0004 §2）。
// The five reasons are the server's closed enum — a `session_paused` carrying
// anything else is rejected with `invalid_pause_reason`, so the list is
// rendered straight from the shared contract rather than retyped here.
//
// Only `manual` counts against the student; the other four are technical
// pauses excluded from scoring (ADR-0002 §6, §8), which is why the operator is
// asked to classify rather than just "pause".

const REASON_LABELS: Record<PauseReason, string> = {
  manual: "人工暫停 manual（計入流程）",
  network_failure: "網路異常 network_failure",
  clock_sync_failure: "時鐘同步失敗 clock_sync_failure",
  recording_failure: "錄影／錄音失敗 recording_failure",
  ethical_or_safety_stop: "倫理／安全中止 ethical_or_safety_stop",
};

interface Props {
  open: boolean;
  onClose: () => void;
  onPause: (reason: PauseReason) => void;
}

export default function PauseResumeModal({ open, onClose, onPause }: Props) {
  const [reason, setReason] = useState<PauseReason>("manual");

  if (!open) {
    return null;
  }

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 20,
      }}
    >
      <div
        role="dialog"
        aria-label="暫停原因"
        style={{
          width: 420,
          padding: 20,
          background: "#1b1e24",
          border: "1px solid #2c313a",
          borderRadius: 10,
        }}
      >
        <h2 style={{ fontSize: 17, marginTop: 0 }}>暫停 Session · 選擇原因</h2>
        <p style={{ fontSize: 12, color: "#9aa0ab", marginTop: 0 }}>
          技術性原因會在評分時排除（ADR-0002 §6）。
        </p>

        {PAUSE_REASONS.map((value) => (
          <label
            key={value}
            style={{ display: "block", fontSize: 13, padding: "6px 0" }}
          >
            <input
              type="radio"
              name="pause-reason"
              value={value}
              checked={reason === value}
              onChange={() => setReason(value)}
              style={{ marginRight: 8 }}
            />
            {REASON_LABELS[value]}
          </label>
        ))}

        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button
            type="button"
            onClick={() => onPause(reason)}
            style={{ flex: 1, padding: "9px 12px", fontFamily: "inherit" }}
          >
            送出暫停
          </button>
          <button
            type="button"
            onClick={onClose}
            style={{ flex: 1, padding: "9px 12px", fontFamily: "inherit" }}
          >
            取消 (Esc)
          </button>
        </div>
      </div>
    </div>
  );
}
