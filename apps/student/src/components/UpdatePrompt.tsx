/** Offers a reload once a newer build is deployed. Never reloads by itself. */
export function UpdatePrompt() {
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
        border: "1px solid #42d392",
        color: "#e8e8ea",
        boxShadow: "0 6px 18px rgba(0, 0, 0, 0.35)",
      }}
    >
      <span>網站已更新到新版本，可以重新整理。</span>
      <button type="button" onClick={() => window.location.reload()}>
        立即更新
      </button>
    </div>
  );
}
