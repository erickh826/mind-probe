# Architecture Decision Records（ADR）— mind-probe

這個資料夾是「不同 AI Agent 就架構問題各自表達意見、再由人整合定案」的固定流程與存檔位置。

## 流程

1. 當出現一個尚無定論的架構問題（例如：WebSocket 重連策略、Fallback 邏輯、時間同步演算法），把問題與相關 spec 章節整理成一段清楚的 context。
2. 分派給至少兩個不同模型（目前用 Claude / GPT-5.5 / Gemini）各自**獨立**、**不互相看對方答案**地給出具體、有立場、不迴避取捨的意見，存成 `opinions/<topic>-<model>.md`。
3. 整合成一份 `NNNN-topic-name.md`，格式包含：狀態、背景、各家意見摘要表、共識、分歧、建議決定。
4. 人（你）看過分歧後拍板，把狀態從「提案中（Proposed）」改成「已定案（Accepted）」，並在文件補上最終決定理由；如果決定跟三家意見都不同，也直接寫清楚為什麼。
5. 定案後的決定寫回 `docs/spec.md` 對應章節，避免兩份文件不同步。

## 目前的 ADR

| 編號 | 主題 | 狀態 |
|---|---|---|
| [0001](0001-websocket-architecture.md) | WebSocket 架構與斷線重連機制 | 已接受（2026-07-22） |
| [0002](0002-fallback-mechanism.md) | Fallback 機制設計 | 已接受（2026-07-22） |
| [0003](0003-time-sync.md) | 時間同步機制 | 已接受（2026-07-22） |
| [0004](0004-cross-cutting-rules.md) | 跨 ADR 共同規定 | 已接受（2026-07-22） |

## 原始意見存檔

`opinions/` 底下是三個模型針對上述三個議題的完整原文意見（未經摘要或修改），供追溯與重新檢視用：

- [opinions/claude.md](opinions/claude.md)
- [opinions/gpt5.md](opinions/gpt5.md)
- [opinions/gemini.md](opinions/gemini.md)
</content>
