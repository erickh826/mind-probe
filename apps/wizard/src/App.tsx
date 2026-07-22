import { useEffect, useState } from "react";
import type { EventEnvelope } from "@mind-probe/shared";

// Wizard控制台（spec §8）：三層結構 分類(F1–F8) → 回答選項 → 語氣狀態。
// Wizard 在兩至三次按鍵內選出回答。This is a scaffold sketching the 3-tier UI.

// 第一層：案件主題分類 (§8)
const CATEGORIES = [
  { key: "F1", label: "身份 Identity" },
  { key: "F2", label: "人物關係 Relationships" },
  { key: "F3", label: "時間線 Timeline" },
  { key: "F4", label: "地點 Location" },
  { key: "F5", label: "證據 Evidence" },
  { key: "F6", label: "矛盾 Contradiction" },
  { key: "F7", label: "程序權利 Procedural rights" },
  { key: "F8", label: "通用回答 General" },
] as const;

// 第三層：語氣狀態 (§8)
const STATES = [
  { key: "N", label: "中性 Neutral" },
  { key: "T", label: "思考 Thinking" },
  { key: "D", label: "防衛 Defensive" },
  { key: "I", label: "不耐煩 Impatient" },
  { key: "R", label: "拒絕 Refusing" },
] as const;

// 必備快捷鍵 (§8)
const HOTKEYS = [
  ["Space", "播放/確認 Play/Confirm"],
  ["Esc", "中止影片 Abort clip"],
  ["I", "返回 Idle"],
  ["T", "播放 Thinking"],
  ["B", "被打斷反應 Barge-in"],
  ["F", "Fallback"],
  ["M", "標記未覆蓋問題 Mark uncovered"],
  ["P", "暫停 session"],
  ["Ctrl+Enter", "結束 session"],
];

export default function App() {
  const [activeCategory, setActiveCategory] = useState<string | null>(null);

  useEffect(() => {
    // TODO(§9): open WebSocket to /ws/{session_id}/wizard; emit clip_command,
    // clip_interrupted, fallback_used, uncovered_question, observer_note, etc.
    // TODO(§8): global keydown handler mapping F1–F8 / 1–6 / N,T,D,I,R + hotkeys.
    const _typeAnchor: EventEnvelope | null = null;
    void _typeAnchor;
  }, []);

  return (
    <div style={{ fontFamily: "system-ui", padding: 16 }}>
      <header style={{ display: "flex", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18 }}>Wizard 控制台</h1>
        {/* TODO: current case phase indicator (§3.2 B). */}
        <span>階段 Phase: <b>—</b></span>
      </header>

      {/* Tier 1: category buttons F1–F8 (§8). */}
      <section>
        <h2 style={{ fontSize: 14 }}>第一層 · 分類 (F1–F8)</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8 }}>
          {CATEGORIES.map((c) => (
            <button
              key={c.key}
              onClick={() => setActiveCategory(c.key)}
              style={{ padding: 12, fontWeight: activeCategory === c.key ? 700 : 400 }}
            >
              <kbd>{c.key}</kbd> {c.label}
            </button>
          ))}
        </div>
      </section>

      {/* Tier 2: answer options for the chosen category (§8). Placeholder list. */}
      <section>
        <h2 style={{ fontSize: 14 }}>第二層 · 回答選項 (1–6)</h2>
        <ol>
          {/* TODO: populate from wizard_decision_tree.json / clip_manifest.json. */}
          <li>選項 1 — placeholder</li>
          <li>選項 2 — placeholder</li>
        </ol>
      </section>

      {/* Tier 3: character state (§8). */}
      <section>
        <h2 style={{ fontSize: 14 }}>第三層 · 語氣狀態</h2>
        <div style={{ display: "flex", gap: 8 }}>
          {STATES.map((s) => (
            <button key={s.key}>
              <kbd>{s.key}</kbd> {s.label}
            </button>
          ))}
        </div>
      </section>

      {/* Suggestion queue + preview/play/abort would live here (§3.2 B, §7). */}

      <footer style={{ marginTop: 16, fontSize: 12, color: "#555" }}>
        <h2 style={{ fontSize: 14 }}>快捷鍵 Hotkeys</h2>
        <ul style={{ columns: 2 }}>
          {HOTKEYS.map(([k, d]) => (
            <li key={k}><kbd>{k}</kbd> — {d}</li>
          ))}
        </ul>
      </footer>
    </div>
  );
}
