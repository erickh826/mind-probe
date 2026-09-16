import {
  CATEGORY_KEYS,
  DEFAULT_CATEGORY_LABELS,
  OPTION_KEYS,
  TONE_KEYS,
  TONE_LABELS,
} from "../types";
import type {
  CategoryKey,
  DecisionTree,
  OptionKey,
  StagedSelection,
  ToneKey,
} from "../types";

// 三層決策樹（spec §8）：分類 F1–F8 → 回答選項 1–6 → 語氣狀態 N/T/D/I/R。
// Every tier is also clickable, but the hotkeys are the real interface: spec §8
// budgets two to three keypresses per answer.

interface Props {
  tree: DecisionTree;
  selection: StagedSelection;
  resolvedClipId: string | null;
  disabled: boolean;
  onSelectCategory: (key: CategoryKey) => void;
  onSelectOption: (key: OptionKey) => void;
  onSelectTone: (key: ToneKey) => void;
  onConfirm: () => void;
}

const panelStyle = {
  background: "#1b1e24",
  border: "1px solid #2c313a",
  borderRadius: 8,
  padding: 12,
  marginBottom: 12,
};

const headingStyle = { fontSize: 13, margin: "0 0 8px", color: "#9aa0ab" };

function tierButtonStyle(active: boolean, available: boolean) {
  return {
    padding: "10px 8px",
    fontSize: 13,
    fontFamily: "inherit",
    textAlign: "left" as const,
    color: available ? "#e8e8ea" : "#5c626d",
    background: active ? "#2f5d8a" : "#22262e",
    border: `1px solid ${active ? "#4d8dc9" : "#2c313a"}`,
    borderRadius: 6,
    cursor: available ? "pointer" : "not-allowed",
  };
}

export default function DecisionTreePanel({
  tree,
  selection,
  resolvedClipId,
  disabled,
  onSelectCategory,
  onSelectOption,
  onSelectTone,
  onConfirm,
}: Props) {
  const category = selection.category === null ? undefined : tree[selection.category];
  const option =
    category === undefined || selection.option === null
      ? undefined
      : category.options[selection.option];

  return (
    <div>
      {/* 第一層：主題分類。未被案件決策樹定義的分類以停用樣式呈現。 */}
      <section style={panelStyle}>
        <h2 style={headingStyle}>第一層 · 分類 (F1–F8)</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8 }}>
          {CATEGORY_KEYS.map((key) => {
            const node = tree[key];
            return (
              <button
                key={key}
                type="button"
                disabled={node === undefined}
                onClick={() => onSelectCategory(key)}
                style={tierButtonStyle(selection.category === key, node !== undefined)}
              >
                <kbd>{key}</kbd>{" "}
                {node?.label ?? `${DEFAULT_CATEGORY_LABELS[key]}（未設定）`}
              </button>
            );
          })}
        </div>
      </section>

      {/* 第二層：該分類下的回答選項。 */}
      <section style={panelStyle}>
        <h2 style={headingStyle}>第二層 · 回答選項 (1–6)</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
          {OPTION_KEYS.map((key) => {
            const node = category?.options[key];
            return (
              <button
                key={key}
                type="button"
                disabled={node === undefined}
                onClick={() => onSelectOption(key)}
                style={tierButtonStyle(selection.option === key, node !== undefined)}
              >
                <kbd>{key}</kbd> {node?.label ?? "—"}
              </button>
            );
          })}
        </div>
      </section>

      {/* 第三層：語氣狀態。沒有對應片段的語氣仍可選，但不會解析出 clip_id。 */}
      <section style={panelStyle}>
        <h2 style={headingStyle}>第三層 · 語氣狀態 (N / T / D / I / R)</h2>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {TONE_KEYS.map((key) => {
            const clip = option?.clips[key];
            const authored = typeof clip === "string";
            return (
              <button
                key={key}
                type="button"
                onClick={() => onSelectTone(key)}
                style={{
                  ...tierButtonStyle(selection.tone === key, authored),
                  minWidth: 132,
                }}
              >
                <kbd>{key}</kbd> {TONE_LABELS[key]}
                <div style={{ fontSize: 10, color: "#7d838e" }}>
                  {authored ? clip : "無片段"}
                </div>
              </button>
            );
          })}
        </div>
      </section>

      {/* 暫存選擇預覽 + Space 確認。 */}
      <section
        style={{
          ...panelStyle,
          display: "flex",
          alignItems: "center",
          gap: 16,
          borderColor: resolvedClipId === null ? "#2c313a" : "#4d8dc9",
        }}
      >
        <div style={{ flex: 1 }}>
          <h2 style={headingStyle}>待送出 Staged</h2>
          <div style={{ fontSize: 14 }}>
            {selection.category ?? "—"} / {selection.option ?? "—"} /{" "}
            {selection.tone ?? "—"}
            <span style={{ marginLeft: 12, color: "#9aa0ab" }}>
              {resolvedClipId ?? "尚未解析出 clip_id"}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onConfirm}
          disabled={disabled || resolvedClipId === null}
          style={{
            padding: "10px 18px",
            fontSize: 14,
            fontFamily: "inherit",
            cursor: resolvedClipId === null ? "not-allowed" : "pointer",
          }}
        >
          <kbd>Space</kbd> 播放 / 確認
        </button>
      </section>
    </div>
  );
}
