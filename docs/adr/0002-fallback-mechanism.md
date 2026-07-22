# ADR-0002：Fallback 機制設計

## 狀態
已接受（Accepted）

Decision date: 2026-07-22
Decision owner: GPT-5.6-Sol
Supersedes: none

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
- 都同意 fallback 事件本身是重要研究資料，必須結構化記錄成 uncovered question。
- 都同意 WebSocket 斷線時學生端錄影必須完全獨立於連線狀態，繼續錄影並持續走 IndexedDB 佇列上傳。

## 分歧
1. **Fallback 怎麼選——三者差異最大之處**：Claude 主張優先用「相近的案件專屬片段」；GPT-5.5 主張固定四類由 Wizard 手動選；Gemini 主張完全不讓 Wizard 選，Server 自動輪替。這反映一個無法同時最大化的三方取捨：回答語境貼合度（Claude）vs 分類清晰度（GPT-5.5）vs Wizard 操作簡單度（Gemini）。
2. **斷線時是否告知學生**：Claude、GPT-5.5 都主張顯示某種「連線中斷但持續錄影」提示（差異只在措辭）；Gemini 主張完全隱藏、無縫接到 Idle 循環——這涉及研究倫理：對受試學生隱瞞系統實際發生的技術狀況，可能影響研究誠信與同意書的有效性。
3. **升級路徑**：GPT-5.5 有明確的「同 intent 重複次數 → 主持人介入」機制且記錄 `operator_intervention`；Claude、Gemini 都沒有提出對稱的主持人介入機制。

## 最終決議（Accepted）

> 以下為人工拍板後的最終決定，在「Wizard 選擇的自由度」與「回答內容的可控性」之間取一個混合模式：Wizard 選語義類別、Server 依案件狀態解析實際影片，明確拒絕 Gemini 的全自動輪替方案。

### 1. 誰選擇 Fallback？
採用混合責任模式：**Wizard 選擇 fallback 的語義類別，Server 根據案件狀態選擇實際獲批准的影片**。

不採用：Gemini 提出的完全自動輪替；Wizard 任意從所有影片中自由選擇；自動選擇語義不確定的「最近似案件回答」。

理由：自動輪替可能答非所問；會影響學生接下來的訪談方向；不同學生可能收到不一致回覆；可能無意間披露案件事實；不利於研究審計。

### 2. 固定四類 Fallback
初版採用以下四類，Wizard 按的是語義類別，不是固定影片 ID：

| ID | 類別 | 用途 |
|---|---|---|
| `CLARIFY` | 要求澄清 | 問題含糊、不知道指哪一件事 |
| `ONE_AT_A_TIME` | 要求逐一提問 | 學生一次提出多個問題 |
| `UNKNOWN_OR_UNSURE` | 不知道／不確定 | 角色確實不知道或記憶不清 |
| `DECLINE_OR_BOUNDARY` | 拒答／程序界線 | 角色不願回答、問題超出可披露範圍 |

```json
{
  "command_type": "request_fallback",
  "payload": {"fallback_category": "CLARIFY", "reason": "ambiguous_question"}
}
```

Server 根據案件狀態、角色情緒、合作程度、前一次 fallback、已披露事實，解析為實際 clip：

```json
{"fallback_category": "CLARIFY", "resolved_clip_id": "GEN_CLARIFY_DEFENSIVE_01"}
```

### 3.「近似案件片段」不屬於 Fallback
如果已有語義上合適、且案件狀態允許的案件回答，應作為正常 Response 播放，而非 fallback。只有在沒有適合的案件回答、問題不清楚、問題超出角色知識、學生一次提出多題、或角色合理拒絕時，才使用 fallback——這樣才能準確量度影片覆蓋率。

### 4. 禁止 Server 自動輪替 Fallback
Server 可以避免連續播放完全相同的影片，但不得自行改變語義類別。可允許在同一 `CLARIFY` 類別內選擇另一個已批准措辭；不允許自動轉為拒絕回答或自動轉為「不記得」——後者會改變角色所表達的事實和態度。

### 5. 連續 Fallback 限制
同一學生問題最多使用一次 fallback；連續兩次學生發問都要使用 fallback 時，Wizard 端顯示警告；連續三次 fallback 時，系統建議主持人暫停；不允許透過不斷播放 fallback 來掩蓋系統無法繼續。

```json
{"event_type": "fallback_threshold_reached", "payload": {"consecutive_fallbacks": 3}}
```

### 6. Fallback 是否影響學生評分？
初版中，**Fallback 本身不自動影響學生分數**——原因可能是影片庫不足、Wizard 找不到回答、學生問題確實不清楚、系統延遲、案件設計缺漏。教師回看時可以判斷學生提問是否有問題，但不能直接採用「使用 fallback 一次 = 學生扣分」的規則。

### 7. Fallback 記錄要求
每次 fallback 必須記錄：

```json
{
  "event_type": "fallback_used",
  "payload": {
    "fallback_category": "CLARIFY",
    "resolved_clip_id": "GEN_CLARIFY_01",
    "reason": "ambiguous_question",
    "wizard_selected": true,
    "question_segment_id": "SEG-041"
  }
}
```

可選 Wizard 原因：`no_matching_response`、`ambiguous_question`、`compound_question`、`outside_character_knowledge`、`disclosure_not_allowed`、`wizard_could_not_find_clip`、`technical_delay`。這對於後續區分「內容缺口」和「學生提問問題」非常重要。

### 8. 斷線時是否讓學生知道？（採 GPT-5.5／Claude 方向，拒絕 Gemini 的隱藏方案）
拒絕「完全隱藏斷線」的方案，採用分級處理：

- **短暫異常（2 秒以內）**：可繼續播放 Idle／Thinking，不顯示技術錯誤，嘗試自動恢復，僅記錄網路異常——這屬於平滑使用者體驗，不是假裝角色給出了新的心理反應。
- **超過 2 秒或狀態不確定**：必須顯示中性提示「系統正在重新連線，請稍候。訪談計時已暫停。」同時暫停正式訪談計時、禁止 Wizard 發出新回答、不把等待算入學生沉默時間；本地學生錄影可繼續但標記為技術暫停；恢復後由 Server snapshot 重新協調。
- **超過 30 秒**：進入 `connection_lost`，由主持人決定恢復、重新開始當前問題、中止 session，或標記為技術失敗。

### 9. 研究倫理決定
可以在經倫理審批的 Wizard of Oz 研究中，暫時不披露幕後人工控制；但不能向參與者隱藏會實質影響表現或評分的技術故障。測試後必須 debrief：解釋哪些部分由 Wizard 控制、是否發生過技術中斷、允許參與者撤回研究資料；技術中斷期間的行為不得計入正式能力判斷。若將來用於正式課程評分，應在測試前說明系統包含人工或 AI 輔助、技術故障會如何處理、發生故障時是否重做，且教師保留最終評分權。

### 10. 即時 TTS
初版繼續禁止即時 TTS fallback，理由是聲音不一致、生成延遲不穩定、可能生成案件外內容、破壞 Wizard 測試的標準化。找不到回答時只能：選擇四類 fallback 之一、標記未覆蓋問題、必要時暫停測試、測試後補充影片。

> 跨 ADR 共同規定（session pause reason、審計記錄等）見 [0004-cross-cutting-rules.md](0004-cross-cutting-rules.md)。此決定已同步回 `docs/spec.md`。
</content>
