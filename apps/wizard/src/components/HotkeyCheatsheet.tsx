import { useState } from "react";

// 快捷鍵速查（spec §8）。
//
// Note the documented collision rule: `T` and `I` are tier-3 tone keys *and*
// global playback hotkeys. Spec §8 lists both and does not resolve the
// overlap, so this console resolves it by context — see `TIER3_NOTE`.

const GLOBAL_HOTKEYS: Array<[string, string]> = [
  ["Space", "播放／確認暫存的 clip_command"],
  ["Esc", "中止播放中的片段（return_to_idle）／關閉彈窗"],
  ["F1–F8", "第一層 · 主題分類"],
  ["1–6", "第二層 · 回答選項"],
  ["N / T / D / I / R", "第三層 · 語氣狀態（已選回答選項時）"],
  ["I", "返回 Idle（未暫存回答選項時）"],
  ["T", "播放 Thinking 片段（未暫存回答選項時）"],
  ["B", "學生插話打斷（clip_interrupted）"],
  ["F", "送出 CLARIFY fallback"],
  ["M", "標記未覆蓋問題"],
  ["P", "暫停 Session（選擇原因）"],
];

const TIER3_NOTE =
  "已選擇第二層回答選項時，N/T/D/I/R 視為語氣；未選擇時 T = Thinking 片段、I = 返回 Idle。";

export default function HotkeyCheatsheet() {
  const [open, setOpen] = useState(false);

  return (
    <section
      style={{
        background: "#1b1e24",
        border: "1px solid #2c313a",
        borderRadius: 8,
        padding: 12,
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        style={{
          background: "none",
          border: "none",
          color: "#9aa0ab",
          fontFamily: "inherit",
          fontSize: 13,
          padding: 0,
          cursor: "pointer",
        }}
      >
        {open ? "▾" : "▸"} 快捷鍵 Hotkeys
      </button>

      {open && (
        <>
          <ul style={{ fontSize: 12, lineHeight: 1.7, paddingLeft: 18, margin: "8px 0" }}>
            {GLOBAL_HOTKEYS.map(([key, description]) => (
              <li key={key}>
                <kbd>{key}</kbd> — {description}
              </li>
            ))}
          </ul>
          <p style={{ fontSize: 11, color: "#9aa0ab", margin: 0 }}>{TIER3_NOTE}</p>
          <p style={{ fontSize: 11, color: "#9aa0ab", margin: "4px 0 0" }}>
            輸入框聚焦時所有快捷鍵停用。
          </p>
        </>
      )}
    </section>
  );
}
