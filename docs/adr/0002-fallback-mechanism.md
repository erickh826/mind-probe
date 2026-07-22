# ADR-0002：Fallback 機制設計

## 狀態
提案中（Proposed）— 三個模型在「Wizard 要不要自己選 fallback 類別」與「斷線時要不要讓學生知道」這兩點上是直接衝突（不只是細節差異），是這三個 ADR 裡最需要你親自拍板、且涉及研究倫理判斷的一項。

## 背景
依 `docs/spec.md` 第六、七、八、十一節：初版有 4 段通用 Fallback 影片、`F`（Fallback）與 `M`（標記未覆蓋問題）熱鍵，但規格未定義：Wizard 該如何判斷該用案件專屬回答還是通用 Fallback、Fallback 內部四段影片如何挑選、WebSocket 斷線時學生端該顯示什麼、以及真的卡住時的升級路徑。

## Agent 意見摘要

| 模型 | Fallback 選擇方式 | 斷線時 UI／學生是否知情 | 升級路徑 |
|---|---|---|---|
| Claude | Wizard 先判斷「沾邊但不精確」vs「完全沒有對應」；優先選相近的**案件專屬片段**（用 `forbidden_after` 過濾避免事實矛盾），非直接跳通用 fallback | 顯示「連線中斷，錄影持續進行中」提示學生 | 不做 session 內真人聲音介入；`M` 標記跨 session 累積，事後補拍新片段 |
| GPT-5.5 | 固定四類（`clarify`／`memory_gap`／`refuse_or_deflect`／`pressure_reaction`），Wizard 依明確規則手動選類別 | 顯示「連線中斷，錄影仍在本機保存，請等待主持人指示」 | 同一 intent 第 2 次強制補 note；第 3 次或核心事實卡住時 Wizard 可按 `P` 暫停，記錄 `operator_intervention` 由主持人介入說明 | 
| Gemini | **反對** Wizard 自己選類別——單一 `F` 熱鍵，Server 端自動依序循環（round-robin）播 4 段 fallback，理由是降低 Wizard 認知負荷 | **嚴禁**顯示任何斷線提示，畫面無縫切到 `Idle` 循環，對學生完全隱藏斷線事實 | 同 session 第 3 次 fallback 時 UI 強制高亮「程序反應／終止」按鈕，暗示 Wizard 主動結束訪談 |

完整原文：[opinions/claude.md](opinions/claude.md) · [opinions/gpt5.md](opinions/gpt5.md) · [opinions/gemini.md](opinions/gemini.md)

## 共識
- 都反對初版加入即時 TTS、即時 LLM 生成回答，或 Wizard／主持人現場真人聲音代答。
- 都同意 fallback 事件本身���重要研究資料，必須結構化記錄成 uncovered question。
- 都同意 WebSocket 斷線時學生端錄影必須完全獨立於連線狀態，繼續錄影並持續走 IndexedDB 佇列上傳。

## 分歧（需要你決定）
1. **Fallback 怎麼選——三者差異最大之處**：Claude 主張優先用「相近的案件專屬片段」；GPT-5.5 主張固定四類由 Wizard 手動選；Gemini 主張完全不讓 Wizard 選，Server 自動輪替。這反映一個無法同時最大化的三方取捨：**回答語境貼合度（Claude）vs 分類清晰度（GPT-5.5）vs Wizard 操作簡單度（Gemini）**。值得注意：Gemini 的立場其實最貼合規格本身把「Wizard 認知負荷過高」列為最高優先風險的態度，但代價是完全犧牲語境貼合度。
2. **斷線時是否告知學生**：Claude、GPT-5.5 都主張顯示某種「連線中斷但持續錄影」提示（差異只在措辭）；Gemini 主張完全隱藏、無縫接到 Idle 循環。**這不只是 UX 選擇，也涉及研究倫理**——對受試學生隱瞞系統實際發生的技術狀況，若學生事後從錄影或系統紀錄察覺自己曾被隱瞞，可能影響研究誠信與同意書的有效性，建議連同研究倫理審查一起確認再定案。
3. **升級路徑**：GPT-5.5 有明確的「同 intent 重複次數 → 主持人介入」機制且記錄 `operator_intervention`；Claude、Gemini 都沒有提出對稱的主持人介入機制（Gemini 提出的是「暗示 Wizard 自己按終止」，屬單方面結束訪談，不是主持人出面說明）。

## 建議決定（僅供參考，需你確認）
- Fallback 選擇：採 Claude 的兩層邏輯（優先近似案件片段＋`forbidden_after` 過濾），但把 GPT-5.5 的四類命名法套用在「真的需要跳到通用 fallback」這一層，兩者結合而非互斥使用。
- 斷線提示：採 GPT-5.5／Claude 的立場，顯示低調的「連線中斷，錄影持續進行」提示。**不建議**採用 Gemini 的完全隱藏方案，理由是隱藏技術故障在教育研究情境下有倫理風險。
- 升級路徑：採用 GPT-5.5 的「同 intent 重複次數觸發主持人介入」機制，並記錄 `operator_intervention` 事件；不採用 Gemini「暗示 Wizard 自行終止」的單方面做法。

> 此為整合三方意見後的建議，非最終定案，尤其「斷線是否告知學生」建議你額外諮詢研究倫理審查意見後再拍板。
</content>
