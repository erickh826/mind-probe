# ADR-0003：時間同步機制

## 狀態
提案中（Proposed）— 分歧點多數是可以憑統計學/工程常識相對客觀判斷的技術問題，不需要曠日辯論，適合快速定案。

## 背景
依 `docs/spec.md` 第九節：Server 與 Client 用多次 ping/pong 估算 clock offset，目標「主要事件與學生錄影之間誤差低於 300ms」，但規格未定義具體取樣次數、統計方法（median/average）、是否需要 session 進行中的持續校正，以及備用 tone+flash 同步法的定位。

## Agent 意見摘要

| 模型 | Ping/Pong 次數 | 統計方法 | 連續 Drift Correction | Tone+Flash 定位 |
|---|---|---|---|---|
| Claude | 7–9 次 | Median，丟棄 RTT 最大 2 筆 | 不做，僅 session 中點（7–8分鐘）背景複測一次作保險，差異>50ms 才記錄警示 | 一次性錨點，只給備用錄影用，非持續同步機制 |
| GPT-5.5 | 12 次 | Median + MAD 離群值過濾（丟 RTT 最高 25%） | 要做，每 60 秒背景 5 次 probe；差異 30–150ms 平滑修正（10秒內套用）；>150ms 標記 `clock_resync_jump` 警示 | 開始＋結束各一次，仍是備援非主機制 |
| Gemini | 5 次 | 剃除最高最低各 1 筆後取**平均值**（明確不用 median） | 明確反對，鎖定初始 offset 直到 session 結束 | 主張應為「強制執行的第一道保險」，非單純備用，session 開始第 0 秒強制執行 |

完整原文：[opinions/claude.md](opinions/claude.md) · [opinions/gpt5.md](opinions/gpt5.md) · [opinions/gemini.md](opinions/gemini.md)

## 共識
- 都同意 <300ms 同步目標在同一區網、10–15 分鐘場景下可行。
- 都同意不需要 NTP daemon、PTP 硬體或 WebRTC clock sync（對 MVP 是過度工程）。
- 都同意 tone+flash 對備用攝影機的事後對齊有價值。

## 分歧
1. **Median vs Average**：Claude、GPT-5.5 都主張用 median（統計上對離群值更穩健，是有離群值風險的網路量測情境下的業界共識）；Gemini 主張用平均值。**這是三者中最技術性、最有客觀答案的分歧**——值得注意 Gemini 選的取樣次數（5次）恰好是三者最少的，取樣數越少 median 的統計效力越弱，等於自己選了一個讓 median 相對不利的取樣策略後再主張用 average，這個推論鏈需要留意，不建議直接採信。
2. **是否做連續 Drift Correction**：GPT-5.5 主張要（每 60 秒背景複測+平滑修正）；Claude 主張中點複測一次作保底即可；Gemini 主張完全不需要。三者都同意區網 15 分鐘內的物理時鐘漂移很小（<30–50ms），差異在於「小到可忽略」還是「小但仍該監控」的風險偏好，不是事實分歧。
3. **Tone+Flash 的角色**：Gemini 主張應強制作為「第一道保險」而非單純備用；Claude、GPT-5.5 較保守，維持 spec 原文「備用／事後對齊」定位。

## 建議決定（僅供參考，需你確認）
- 取樣次數與統計法：採 GPT-5.5 的 **12 次 + median + MAD 離群值過濾**，這個組合在統計上最站得住腳（median 優於 average 有共識，取樣次數也比另兩家更充足，離群值處理比單純丟固定筆數更嚴謹）。
- Drift Correction：採 Claude 的折衷方案——**不做連續動態校正，但在 session 中點做一次背景複測作保險**，差異超過閾值只記錄警示、不動態修正歷史事件。理由：GPT-5.5 的連續修正機制在 MVP 階段增加的複雜度與除錯難度，相對於 15 分鐘場景的實際收益不成比例；但完全不複測（Gemini 方案）又略嫌武斷。
- Tone+Flash：維持 spec 原文定位（備用／事後對齊，非強制的第一道保險）。不採用 Gemini 的升級提案，理由是強制閃光/嗶聲在 session 開始時可能影響學生的沉浸式初始體驗，且對主同步機制沒有實質補強。

> 此為整合三方意見後的建議，非最終定案。技術分歧建議工程團隊快速拍板（不需要外部審查），倫理相關的分歧請參考 ADR-0002。
</content>
