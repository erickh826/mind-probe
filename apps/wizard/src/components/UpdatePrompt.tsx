interface UpdatePromptProps {
  /** Extra caution, e.g. while a session is running. */
  note: string | null;
}

/** Offers a reload once a newer build is deployed. Never reloads by itself. */
export function UpdatePrompt({ note }: UpdatePromptProps) {
  return (
    <div
      role="status"
      style={{
        position: "fixed",
        top: "12px",
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        gap: "12px",
        padding: "10px 14px",
        borderRadius: "8px",
        background: "#1d2733",
        border: "1px solid #f2c14e",
        color: "#e8e8ea",
        fontFamily: "system-ui",
        boxShadow: "0 6px 18px rgba(0, 0, 0, 0.35)",
      }}
    >
      <span>
        網站已更新到新版本。
        {note ? <strong style={{ marginLeft: "6px", color: "#f2c14e" }}>{note}</strong> : null}
      </span>
      <button type="button" onClick={() => window.location.reload()}>
        重新整理
      </button>
    </div>
  );
}
