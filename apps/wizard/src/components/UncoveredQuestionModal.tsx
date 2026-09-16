import { useState } from "react";
import type { UncoveredQuestionPayload } from "@mind-probe/shared";

// 未覆蓋問題標記（spec §8 `M` 熱鍵、ADR-0002 §7）。
// Wizard-only, and broadcast to teacher/observer rather than the student. The
// server persists the payload verbatim without validating its shape, so every
// field here is optional — this form is the agreed annotation vocabulary.

interface Props {
  open: boolean;
  onClose: () => void;
  onSubmit: (payload: UncoveredQuestionPayload) => void;
  /** Session-relative ms, stamped onto the annotation when available. */
  sessionElapsedMs: number | null;
}

const fieldStyle = {
  width: "100%",
  marginTop: 4,
  padding: "8px 10px",
  fontFamily: "inherit",
  fontSize: 14,
  boxSizing: "border-box" as const,
};

export default function UncoveredQuestionModal({
  open,
  onClose,
  onSubmit,
  sessionElapsedMs,
}: Props) {
  const [questionText, setQuestionText] = useState("");
  const [note, setNote] = useState("");
  const [segmentId, setSegmentId] = useState("");

  if (!open) {
    return null;
  }

  function submit() {
    const payload: UncoveredQuestionPayload = {
      ...(questionText.trim() ? { question_text: questionText.trim() } : {}),
      ...(note.trim() ? { note: note.trim() } : {}),
      ...(segmentId.trim() ? { question_segment_id: segmentId.trim() } : {}),
      ...(sessionElapsedMs === null ? {} : { timestamp_ms: sessionElapsedMs }),
    };
    onSubmit(payload);
    setQuestionText("");
    setNote("");
    setSegmentId("");
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
        aria-label="標記未覆蓋問題"
        style={{
          width: 460,
          padding: 20,
          background: "#1b1e24",
          border: "1px solid #2c313a",
          borderRadius: 10,
        }}
      >
        <h2 style={{ fontSize: 17, marginTop: 0 }}>標記未覆蓋問題</h2>
        <p style={{ fontSize: 12, color: "#9aa0ab", marginTop: 0 }}>
          影片庫無法回答的學生提問。只會送給教師／觀察者。
        </p>

        <label style={{ display: "block", fontSize: 12, marginBottom: 10 }}>
          學生問題 question_text
          <textarea
            value={questionText}
            onChange={(e) => setQuestionText(e.target.value)}
            rows={3}
            style={fieldStyle}
            autoFocus
          />
        </label>

        <label style={{ display: "block", fontSize: 12, marginBottom: 10 }}>
          Wizard 備註 note
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            style={fieldStyle}
          />
        </label>

        <label style={{ display: "block", fontSize: 12, marginBottom: 10 }}>
          提問片段 ID question_segment_id（可選）
          <input
            value={segmentId}
            onChange={(e) => setSegmentId(e.target.value)}
            style={fieldStyle}
          />
        </label>

        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button
            type="button"
            onClick={submit}
            style={{ flex: 1, padding: "9px 12px", fontFamily: "inherit" }}
          >
            送出標記
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
