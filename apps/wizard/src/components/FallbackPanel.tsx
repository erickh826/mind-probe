import { useState } from "react";
import { FALLBACK_CATEGORIES, FALLBACK_REASONS } from "@mind-probe/shared";
import type { FallbackCategory, FallbackReasonValue } from "@mind-probe/shared";
import { FALLBACK_CATEGORY_LABELS } from "../types";

// ADR-0002 §2/§5：Wizard 只選語意類別，由 Server 決定實際片段。
// The reason picklist is deliberately open (ADR-0002 §7 calls these 可選
// reasons) — the server records whatever arrives rather than refusing a live
// fallback over a taxonomy value.

interface Props {
  /** ADR-0002 §5 streak, as reported by the server. */
  consecutiveFallbacks: number;
  /** Set once `fallback_threshold_reached` arrives, until dismissed. */
  thresholdAlert: number | null;
  onDismissAlert: () => void;
  /** The server rejects `request_fallback` while the session is paused. */
  disabled: boolean;
  onRequestFallback: (
    category: FallbackCategory,
    reason?: FallbackReasonValue,
  ) => void;
}

export default function FallbackPanel({
  consecutiveFallbacks,
  thresholdAlert,
  onDismissAlert,
  disabled,
  onRequestFallback,
}: Props) {
  const [reason, setReason] = useState<string>("");

  return (
    <section
      style={{
        background: "#1b1e24",
        border: "1px solid #2c313a",
        borderRadius: 8,
        padding: 12,
        marginBottom: 12,
      }}
    >
      <h2 style={{ fontSize: 13, margin: "0 0 8px", color: "#9aa0ab" }}>
        Fallback（<kbd>F</kbd> 開啟／第一個類別）· 連續 {consecutiveFallbacks} 次
      </h2>

      {thresholdAlert !== null && (
        <div
          role="alert"
          style={{
            padding: "8px 10px",
            marginBottom: 10,
            background: "#4a2020",
            border: "1px solid #a04444",
            borderRadius: 6,
            fontSize: 13,
          }}
        >
          已連續 {thresholdAlert} 次 fallback — 建議主持人介入並暫停（<kbd>P</kbd>）。
          <button
            type="button"
            onClick={onDismissAlert}
            style={{ marginLeft: 10, fontFamily: "inherit" }}
          >
            知道了
          </button>
        </div>
      )}

      <label style={{ display: "block", fontSize: 12, marginBottom: 10 }}>
        原因 reason（可選）
        <select
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          style={{
            display: "block",
            width: "100%",
            marginTop: 4,
            padding: "6px 8px",
            fontFamily: "inherit",
          }}
        >
          <option value="">（不填）</option>
          {FALLBACK_REASONS.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 8 }}>
        {FALLBACK_CATEGORIES.map((category) => (
          <button
            key={category}
            type="button"
            disabled={disabled}
            onClick={() =>
              onRequestFallback(category, reason === "" ? undefined : reason)
            }
            style={{
              padding: "10px 8px",
              fontSize: 13,
              fontFamily: "inherit",
              cursor: disabled ? "not-allowed" : "pointer",
            }}
          >
            {FALLBACK_CATEGORY_LABELS[category]}
            <div style={{ fontSize: 10, color: "#7d838e" }}>{category}</div>
          </button>
        ))}
      </div>

      {disabled && (
        <p style={{ fontSize: 12, color: "#9aa0ab", margin: "8px 0 0" }}>
          Session 暫停或未連線時無法送出 fallback。
        </p>
      )}
    </section>
  );
}
