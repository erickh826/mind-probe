import { useEffect } from "react";
import type { EventEnvelope } from "@mind-probe/shared";

// Teacher回看端（spec §3.2 D / §13）：雙畫面（學生錄影 + 疑犯片段）＋ 事件時間線
// ＋ rubric 評分。透過 Session Server REST API 讀取資料（審閱後修訂 3）。
// This is a scaffold sketching the dual-view + timeline layout.

// Rubric 評分類別 (§13)，總分 100。
const RUBRIC = [
  { key: "opening", label: "開場及程序說明", max: 10 },
  { key: "rapport", label: "建立關係及尊重", max: 10 },
  { key: "questioning", label: "提問方式", max: 20 },
  { key: "active_listening", label: "主動聆聽及避免打斷", max: 15 },
  { key: "follow_up", label: "跟進細節及矛盾", max: 15 },
  { key: "evidence_strategy", label: "證據展示策略", max: 10 },
  { key: "avoid_coercion", label: "避免誘導、威嚇及不當承諾", max: 10 },
  { key: "closing", label: "總結及結束", max: 5 },
  { key: "reflection", label: "事後反思", max: 5 },
];

export default function App() {
  useEffect(() => {
    // TODO(審閱後修訂 3): fetch session list + events + clip manifest from the
    //   Session Server REST API (GET /cases/{id}, session endpoints).
    // TODO(§3.2 D): replay suspect clips against the event timeline in sync with
    //   the student recording; support seek-to-time and teacher markers.
    const _typeAnchor: EventEnvelope | null = null;
    void _typeAnchor;
  }, []);

  return (
    <div style={{ fontFamily: "system-ui", display: "grid", gridTemplateRows: "1fr auto auto", height: "100vh" }}>
      {/* Dual view: student recording (left) + suspect clip playback (right) (§3.2 D). */}
      <section style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, padding: 8 }}>
        <figure style={{ margin: 0, background: "#111" }}>
          <figcaption style={{ color: "#aaa" }}>學生錄影 Student recording</figcaption>
          <video style={{ width: "100%" }} controls /> {/* TODO: src from REST (student.mp4) */}
        </figure>
        <figure style={{ margin: 0, background: "#111" }}>
          <figcaption style={{ color: "#aaa" }}>疑犯片段 Suspect clips</figcaption>
          <video style={{ width: "100%" }} controls /> {/* TODO: driven by event timeline */}
        </figure>
      </section>

      {/* Event timeline (§3.2 D). Placeholder track. */}
      <section style={{ borderTop: "1px solid #ccc", padding: 8 }}>
        <h2 style={{ fontSize: 14 }}>時間線 Timeline</h2>
        <div style={{ height: 48, background: "#f0f0f0", position: "relative" }}>
          {/* TODO: render clip_started / clip_interrupted / fallback markers. */}
        </div>
      </section>

      {/* Rubric input (§13) + export JSON/CSV (§3.2 D). */}
      <section style={{ borderTop: "1px solid #ccc", padding: 8 }}>
        <h2 style={{ fontSize: 14 }}>評分 Rubric（總分 100）</h2>
        <ul style={{ listStyle: "none", padding: 0 }}>
          {RUBRIC.map((r) => (
            <li key={r.key}>
              {r.label} — <input type="number" min={0} max={r.max} defaultValue={0} /> / {r.max}
            </li>
          ))}
        </ul>
        {/* TODO: export JSON / CSV / scoring report (§3.2 D). */}
        <button>匯出 Export (placeholder)</button>
      </section>
    </div>
  );
}
