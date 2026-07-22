# Mind-Probe 系統架構決策意見書 (ADR)

針對 Mind-Probe 系統初版（MVP）的三大開放性架構爭議，以下提出具體、帶有明確立場的工程決策建議。

## 1. WebSocket 架構與斷線重連機制

**建議做法：**
*   **架構與單一真相來源**：採用星型拓撲（Star-topology），Session Server 作為唯一的狀態與真相來源（Source of Truth）。
*   **Heartbeat 與重連**：設定每 3 秒一次 Ping/Pong heartbeat。若連續 2 次未收到 Pong 即判定斷線。客戶端重連採用指數退避（Exponential backoff），從 500ms 開始，上限為 5s。
*   **斷線重連與事件補送（Event-resend）**：實作基於序號的事件溯源（Event Sourcing）。Server 為每個 session 維護單調遞增的 `sequence` 號碼。當 Wizard 或學生端重連時，在 Auth 訊息中夾帶自己最後收到的 `sequence`，Server 隨即將遺漏的事件完整 Replay。
*   **Session-level Auth**：Server 在創建 Session 時生成一組 256-bit 隨機 Hex 字串作為 `session_token`。Wizard、學生端與回看端皆須透過 URL Query (`?token=...`) 或 WebSocket 建立連線的第一個 Auth 封包提供此 Token。

**主要風險與取捨：**
*   **取捨**：Server 必須在記憶體（或 SQLite）中完整保留 Session 進行中的所有事件以便 Replay，這增加了伺服器端的狀態管理負擔。
*   **風險**：若 Server 實體崩潰重啟，記憶體中的連線狀態與未寫入 DB 的事件會遺失。但因系統限定單機/區域網路運行，此機率極低。

**具體理由：**
在區域網路環境中，最大的不穩定性來自 Wi-Fi 瞬斷而非頻寬不足。採用 `sequence` 號碼進行 Event Replay，是最簡單且保證狀態最終一致性的做法，完全免去了複雜的 State-diffing 邏輯。Wizard 控制台若斷線 5 秒後重連，能瞬間回放這 5 秒內的事件，確保 UI 狀態與 Server 完美對齊。

**信心程度：**
高

**是否有更好的替代方案：**
*   **否定替代方案**：有人可能建議使用 WebRTC Data Channel 讓 Wizard 與學生端 P2P 溝通以降低延遲。我強烈反對。這會繞過 Server，導致事後錄影與事件時間軸難以完美對齊，且增加極大的連線除錯成本。

---

## 2. Fallback 機制設計

**建議做法：**
*   **Fallback 觸發邏輯**：Wizard 遇到未預期問題時，只需按單一「通用 Fallback」熱鍵（如 `F` 鍵）。Server 收到指令後，**依序循環（Sequential Round-Robin）**播放 4 個預設 Fallback 影片（要求澄清 -> 不明白 -> 不記得 -> 拒答），絕對不使用隨機抽取，以避免連續兩次播放同一個 Fallback 影片導致嚴重出戲。
*   **Escalation Path（升級路徑）**：當單一 Session 內觸發第 3 次 Fallback 時，Wizard 控制台的 UI 應強制高亮閃爍「程序反應（要求休息/終止）」按鈕，強烈暗示 Wizard 使用該選項來優雅地結束這場已經脫軌的訪談。
*   **WebSocket 斷線時的系統行為**：若學生端發生斷線，**學生端的 MediaRecorder 必須無條件繼續錄影**，並將 chunk 寫入 IndexedDB 佇列，絕對不可中斷。同時，學生端 UI **嚴禁顯示任何「連線中斷」的錯誤提示**，疑犯畫面應自動無縫切換並無限循環播放 `Idle` 影片。

**主要風險與取捨：**
*   **取捨**：隱藏斷線提示意味著學生可能會對著 `Idle` 的疑犯繼續說話，而 Wizard 此時其實已經斷線聽不到。
*   **風險**：循環 Fallback 的回應可能在語境上不完全契合學生的奇怪問題。

**具體理由：**
初版 MVP 的最高指導原則是「維護 Wizard-of-Oz 的沉浸感」以及「降低 Wizard 認知負荷」。讓 Wizard 在慌亂時還要大腦思考該選哪一種 Fallback 是災難；單一 Panic Button 加上 Server 端決定循環邏輯是最穩妥的。斷線時不報錯並保持本地錄影，能最大程度保全實驗數據，即便缺少了 Wizard 的互動，事後教師仍可評分學生的「提問表現」。

**信心程度：**
高

**是否有更好的替代方案：**
*   **否定替代方案**：讓 Wizard 每次手動選擇要用哪一個 Fallback（澄清 vs 拒絕）。這認知負荷過大。另一種方案：斷線時在畫面上顯示「系統連線異常，請稍後」。這會徹底破壞沉浸感，違背了模擬審訊的初衷。堅持「學生端即使斷線也要裝作沒事，維持錄影」是唯一正解。

---

## 3. 時間同步機制

**建議做法：**
*   **Clock Offset 估算演算法**：在 Session 啟動的準備階段，執行 **5 次** SNTP 風格的 Ping/Pong 來回測試。剃除延遲最高與最低的 2 個極端值（Outlier Filtering），將中間 3 次的 offset 取**平均值（Average）**作為最終的 `clock_offset_ms`。
*   **Drift Correction（時鐘漂移校正）**：**不執行**運行中（Mid-session）的持續漂移校正。一旦在 Session 開始前算出 offset，整個 10-15 分鐘的訪談就絕對鎖定這個數值。
*   **備用同步機制（Tone+Flash）**：這不應該只是備用，而是**強制執行的第一道保險**。Session 開始的第 0 秒，學生端強制全螢幕閃白（White frame）500ms，並同時播放 1000Hz 的嗶聲（Tone）。

**主要風險與取捨：**
*   **取捨**：放棄動態校正會承擔電腦硬體時鐘在 15 分鐘內可能產生的物理漂移。
*   **風險**：如果初始的 5 次 Ping 剛好遇到系統嚴重的 CPU 阻塞，可能會算出錯誤的初始 offset。

**具體理由：**
現代作業系統的石英震盪器（RTC）時鐘漂移率大約是每分鐘 1-2ms。在短短 15 分鐘的 Session 中，物理漂移最多不超過 30ms。這遠低於規格書設定的 `<300ms` 同步誤差目標。如果引入運行中持續的 Ping/Pong 去動態修正時間軸，不僅增加程式複雜度，更可能因為某次網路 GC 延遲導致時間軸突然「跳躍」，這對事後對齊是致命的。
此外，Tone+Flash 提供了絕對物理級別的同步點，就算軟體同步全毀，後製人員或研究員也能輕易在 Premiere / DaVinci 內做到影音軌的幀級別（Frame-perfect）對齊，可靠度極高。

**信心程度：**
高

**是否有更好的替代方案：**
*   **否定替代方案**：每 10 秒進行一次 WebSocket Ping，並使用移動平均線（Moving Average）來動態微調 `server_timestamp_ms`。這在長達數小時的直播系統是必要的，但在 15 分鐘且處於區域網路的 MVP 中，這是典型的過度工程（Over-engineering），只會引入不必要的 Bug 和時間戳跳動問題。