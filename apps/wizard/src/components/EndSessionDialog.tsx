import { useEffect, useRef } from "react";

// 結束場次確認。POST /sessions/{id}/end 會撤銷 live token 並關閉連線，
// 且不會替學生按下 Stop REC，所以先確認再送出。

interface Props {
  open: boolean;
  pending: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}

export default function EndSessionDialog({
  open,
  pending,
  error,
  onClose,
  onConfirm,
}: Props) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) {
      cancelRef.current?.focus();
    }
  }, [open]);

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
        aria-modal="true"
        aria-labelledby="end-session-title"
        style={{
          width: 440,
          padding: 20,
          background: "#1b1e24",
          border: "1px solid #a04444",
          borderRadius: 10,
        }}
      >
        <h2 id="end-session-title" style={{ fontSize: 17, marginTop: 0 }}>
          結束場次
        </h2>
        <p style={{ fontSize: 13, color: "#e8e8ea", lineHeight: 1.6, marginTop: 0 }}>
          確定要結束目前場次嗎？結束後無法從 Wizard 恢復。學生端會看到
          session_ended，Stop REC 仍由學生自行按下。
        </p>

        {error !== null && (
          <div
            role="alert"
            style={{
              padding: "8px 10px",
              marginBottom: 4,
              background: "#4a2020",
              border: "1px solid #a04444",
              borderRadius: 6,
              fontSize: 13,
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            style={{
              flex: 1,
              padding: "9px 12px",
              fontFamily: "inherit",
              fontWeight: 700,
              color: "#fff",
              background: pending ? "#6a3030" : "#8f2d2d",
              border: "1px solid #a04444",
              borderRadius: 6,
              cursor: pending ? "wait" : "pointer",
            }}
          >
            {pending ? "結束中…" : "結束場次"}
          </button>
          <button
            ref={cancelRef}
            type="button"
            onClick={onClose}
            disabled={pending}
            style={{ flex: 1, padding: "9px 12px", fontFamily: "inherit" }}
          >
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
