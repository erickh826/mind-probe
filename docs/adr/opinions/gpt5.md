# mind-probe ADR 意見：三個架構爭議的明確立場

本文基於 `/home/user/workspace/mindprobe_spec_v1_final.md` 的 v1.0 定案規格，針對三個仍需工程決策的架構爭議提出具體、可實作、偏向 MVP 落地的意見。我的總體立場是：**初版不要追求分散式系統的優雅，要追求 10–15 分鐘 session 內「事件不丟、錄影不斷、Wizard 可控、教師可追溯」**。

---

## 1. WebSocket 架構與斷線重連機制

### 建議做法

我建議採用 **Server-authoritative session bus**：Session Server 是唯一權威狀態來源，Wizard console 與 Student app 都只是帶有 role 的 client。不要讓 Wizard 直接控制 Student，也不要讓 Student 自己推斷 session 狀態。

具體設計如下：

1. **單一 session、多 role WebSocket endpoint**
   - Endpoint：`/ws/sessions/{session_id}`。
   - 連線初始化第一個 message 必須是 `auth.resume`：
     ```json
     {
       "type": "auth.resume",
       "session_id": "S001",
       "role": "wizard",
       "client_id": "wizard-console-01",
       "token": "...",
       "last_seen_server_sequence": 128,
       "last_sent_client_sequence": 44
     }
     ```
   - `role` 至少分為 `wizard`、`student`、`reviewer`，MVP 可先只開 `wizard` 與 `student` 的 WebSocket。
   - 每個 role 在同一 session 預設只允許一個 active connection；新連線進來時，舊連線標記為 `superseded` 並關閉，避免兩個 Wizard 同時控制。

2. **ConnectionManager 只管理連線，不管理業務真相**
   - `ConnectionManager` 負責：role → websocket、送訊息、heartbeat、disconnect 標記。
   - `SessionState` 負責：目前 clip、session status、wizard state、student recorder state。
   - `EventStore` 負責：SQLite append-only event log、server sequence、client event 去重。
   - 不要把狀態藏在 WebSocket 物件內；WebSocket 斷線後，狀態仍必須完整存在於 Server 與 SQLite。

3. **事件採 server sequence 為主、client sequence 為輔**
   - 每個 server 廣播事件都必須有全 session 遞增的 `server_sequence`。
   - 每個 client 發出的事件要有：`client_id`、`client_sequence`、`event_id`。
   - Server 以 `event_id` 做 idempotency；重連補送時收到同一事件不得重複寫入。
   - 規格已有 `sequence` 欄位，我建議明確改名或語意固定為 `server_sequence`；如果不改名，至少文件內要寫死：`sequence` 是 Server 指派，不是 client 自行產生。

4. **重連流程必須是 resume，不是重新加入**
   - Client 偵測斷線後自動重連，backoff：250ms、500ms、1s、2s，之後維持 2s 加 jitter。
   - 重連時送 `last_seen_server_sequence`。
   - Server 回覆：
     ```json
     {
       "type": "resume.ok",
       "session_snapshot": {
         "status": "running",
         "current_clip_id": "C001_EVIDENCE_04",
         "clip_state": "playing",
         "clip_started_server_ms": 52340,
         "recording_state": "local_recording_server_disconnected"
       },
       "missed_events": [ ... ]
     }
     ```
   - **關鍵點：不要盲目重播 missed play command。** 重連後必須先套用 `session_snapshot`，再把 missed events 補入 log。否則 Student 可能在 20 秒後錯誤重播一段已經過期的影片。

5. **heartbeat cadence**
   - 使用 application-level heartbeat，不只依賴 TCP/WebSocket ping。
   - Server 每 5 秒送 `heartbeat.ping`，client 立即回 `heartbeat.pong`。
   - 連續 3 次未回，即約 15 秒，判定該 client `disconnected`。
   - UI 層面不要等 15 秒才反應：超過 6 秒未收到 heartbeat，即顯示「連線不穩，但錄影仍在本機進行」。

6. **錄影 chunk 不要走 WebSocket 主通道**
   - WebSocket 只傳控制事件與小型 metadata。
   - MediaRecorder chunk 用 HTTP `POST /sessions/{id}/recording/chunks` 上傳。
   - chunk 上傳失敗進 IndexedDB，背景重試。
   - 這可以避免 2 秒一次的大型 binary chunk 阻塞 Wizard 播片指令。

7. **session-level token 設計**
   - MVP 不需要完整帳號系統，但不能只靠同網段信任。
   - Server 建 session 時產生一個高 entropy session secret；操作員輸入此 secret 後，Server 派發 role-scoped token。
   - Token claims：`session_id`、`role`、`client_id`、`issued_at`、`expires_at`。
   - Wizard token 只能發控制事件；Student token 只能上傳錄影與回報播放狀態；Reviewer token 只能讀取 REST API。
   - Token 可用 server-side opaque token，更簡單、更好撤銷；MVP 不必上 JWT。
   - WebSocket token 可以放第一個 auth message，不建議長期放 query string，避免被 proxy 或 browser history 意外記錄。

### 主要風險與取捨

1. **比「直接廣播」複雜**：server sequence、resume、snapshot、idempotency 會增加約 2–4 天工程量，但這是防止關鍵事件遺失的最低成本。
2. **SQLite append-only event log 需要嚴格 discipline**：所有事件都要經同一路徑寫入，不能有某些狀態只存在 memory。
3. **單一 Session Server 是單點故障**：但 MVP 在同一 LAN、單 session、10–15 分鐘使用情境下，這個取捨合理；不要為初版引入 Redis、Kafka 或多 server replication。
4. **role-scoped token 比單 token 麻煩**：但它可以避免學生端拿到 Wizard 權限，這是低成本安全底線。

### 具體理由

這個系統的真正難點不是 WebSocket 本身，而是 **斷線後是否仍能重建一條可信時間線**。教師回看、評分、研究資料收集都依賴完整事件 log；因此 server sequence、snapshot resume、idempotent resend 是核心，不是加分功能。

我反對把架構做成「Wizard 按鍵 → WebSocket 直接送 Student → Student 播片 → 有空再通知 Server」。這會讓 Server 事後只能猜實際發生了什麼，斷線時尤其危險。正確方向是「Wizard 發 intent 給 Server → Server 記錄並指派 sequence → Server 發 command 給 Student → Student 回報實際播放狀態」。

### 信心程度

**高。** 這是最符合 MVP 風險分布的設計：不過度工程化，但把事件可靠性放在第一位。

### 是否有更好的替代方案

有一個替代方案是使用 Socket.IO 這類帶 room、ack、reconnect 語意的框架。但我不建議初版採用，因為 FastAPI 原生 WebSocket 加少量自訂 protocol 已足夠，而且你們需要的是 domain-level resume，不只是 transport-level reconnect。

另一個替代方案是引入 Redis Streams 作為 event bus。我也不建議初版使用；單機 SQLite append-only log 已可滿足單 session / LAN / MVP。等到多房間、多 session 同時運作時，再升級到 Redis Streams 或 Postgres logical event table。

---

## 2. Fallback 機制設計

### 建議做法

我的立場很明確：**Fallback 不應該假裝回答問題；Fallback 的責任是保持角色一致、不中斷錄影、標記資料缺口，並把 session 優雅地帶回可控狀態。** 初版絕對不要加入即時 TTS、即時 LLM 生成答案或 Wizard 現場自由輸入台詞。

具體設計如下：

1. **Fallback clip 固定四類，且每類有明確使用規則**
   - `clarify`：學生問題不清楚、太長、含糊、Wizard 無法判斷 intent。例：「你講清楚啲，你問邊一段時間？」
   - `memory_gap`：問到細節但案件允許疑犯不記得。例：「我真係唔記得咁細節。」
   - `refuse_or_deflect`：問到自證其罪、程序權利、敏感動機、疑犯可合理拒答。例：「呢個我唔想答。」
   - `pressure_reaction`：學生重複追問、打斷、施壓或情緒升高。例：「你唔好一直屈我。」

2. **Fallback 流程固定化**
   - Student 問完。
   - Wizard 找不到匹配 clip。
   - Wizard 先按 `T` 播 Thinking，爭取 1–3 秒。
   - 仍找不到，按 `M` 標記 uncovered question。
   - Wizard 選一個 fallback 類別，或直接按 `F` 開啟四選一 fallback overlay。
   - Server 寫入：`uncovered_question_marked`、`fallback_selected`、`clip_started`。
   - 播完 fallback 後回 Idle。

3. **uncovered question 必須結構化記錄**
   - 事件 payload 至少包含：
     ```json
     {
       "phase": "timeline",
       "wizard_guess_intent": "ask_unknown_alibi_detail",
       "fallback_type": "memory_gap",
       "student_question_start_client_ms": 81220,
       "student_question_end_client_ms": 86410,
       "wizard_note": "Asked about exact bus route, no clip exists"
     }
     ```
   - 如果 Wizard 來不及打字，先用 hotkey 標記，session 後補 note。
   - 教師回看端必須能 filter 所有 uncovered question，因為這是下一輪補拍影片的主要依據。

4. **Fallback 選擇邏輯以「不矛盾」優先於「像回答」**
   - 若問題涉及案件事實，而沒有對應 clip，優先選 `clarify` 或 `memory_gap`。
   - 若問題涉及程序權利或拒答合理性，選 `refuse_or_deflect`。
   - 若學生連續追打同一缺口兩次，選 `pressure_reaction`，不要一直重複同一段 `clarify`。
   - Wizard console 應顯示 fallback 使用次數；同一 session fallback 超過 3 次時提示主持人注意，但不要自動中止。

5. **Escalation path 要明確，但不能破壞研究資料**
   - 第一次 uncovered：正常 fallback，繼續。
   - 同一 intent 第二次 uncovered：fallback + 強制 note。
   - 同一 intent 第三次 uncovered 或案件核心事實卡住：Wizard 可按 `P` 暫停 session，由主持人介入說明「系統需要暫停」，這要記錄 `operator_intervention`。
   - 不允許 Wizard 現場自己講答案、不允許主持人扮演疑犯補答；那會污染訓練與評分資料。

6. **WebSocket mid-session disconnect 時 Student 必須繼續本機錄影**
   - Student app 的 MediaRecorder 生命週期必須獨立於 WebSocket。
   - WebSocket 斷線時：
     - MediaRecorder 繼續錄。
     - chunk 繼續寫入 IndexedDB。
     - 上傳背景重試。
     - 本地寫入 `connection_lost_local` event queue。
     - UI 顯示「連線中斷，錄影仍在本機保存，請等待主持人指示」。
   - 如果斷線時正在播 clip，clip 可以播完；播完後進入 local Idle / holding state，不要自己選下一段。
   - 重連後：先 resume + snapshot，再補送本地事件與 chunk metadata。
   - Session 結束前，Student app 不得顯示「可安全關閉」，直到所有 chunk 都收到 Server ack。

7. **Wizard disconnect 與 Student disconnect 要分開處理**
   - Wizard 斷線、Student 仍在線：Server 不應自動創造回答；Student 端進 Idle/holding，主持人可口頭暫停。
   - Student 斷線、Wizard 仍在線：Wizard console 顯示紅色狀態，禁止新的 play command，允許標記 note 與暫停。
   - Server 斷線或重啟：Student 本地繼續錄影與排隊，Wizard 停止控制；恢復後以 session token resume。

### 主要風險與取捨

1. **Fallback 會降低自然度**：但比錯誤回答或突然 TTS 變聲更可接受。
2. **Wizard 要多按 `M` 和 fallback 類別**：會增加操作負荷，所以 hotkey 與 overlay 必須非常快；但這些標記是後續內容迭代的核心資料。
3. **Student 斷線仍錄影會產生大量本地暫存**：需要 IndexedDB quota 與上傳進度 UI；但 10–15 分鐘 WebM chunk 在 Chrome 可控。
4. **暫停介入會打斷沉浸感**：所以只應作為第三次同 intent 卡住或核心事實卡住時的 escalation，不是一般 fallback。

### 具體理由

初版的研究價值不是讓每個問題都被完美回答，而是找出「學生實際會問什麼」與「現有影片庫缺什麼」。因此 fallback 事件不是失敗資料，而是最重要的產品資料之一。

我反對在 fallback 場景加入即時 LLM/TTS，因為它會同時引入三個風險：聲音一致性破壞、案件事實幻覺、反應時間不可控。這些風險都直接打擊 MVP 的三個核心目標：Wizard 可控、事件可同步、案件事實不矛盾。

我也反對 WebSocket 斷線就停止錄影。錄影是研究證據，不應依賴網絡狀態；Server 連不上時，Student app 應降級為「本地錄影器 + 待恢復播放端」。最多損失最後 4 秒的驗收目標，只有在 MediaRecorder 與 IndexedDB 獨立於 WebSocket 時才有機會達成。

### 信心程度

**高。** 這套 fallback 方案犧牲一點自然度，但最大化一致性、可評核性與後續迭代資料品質。

### 是否有更好的替代方案

更好的替代方案不是即時生成，而是 **mini-pilot 後的高頻缺口補拍流程**：每 15–30 個 session 統計 uncovered intents，按頻率與教育價值補拍 5–10 段，逐輪提高覆蓋率。

如果第二版真的要提高 Wizard 找片效率，我建議做「ASR 初稿 + intent classifier + top-3 clip recommendation + Wizard confirm」，而不是 LLM 直接回答。Wizard 必須仍是最後閘門。

---

## 3. 時間同步機制

### 建議做法

我的立場是：**<300ms 同步目標在同一 LAN、Chrome 固定版本、10–15 分鐘 session 中可行，但前提是用 monotonic clock、actual playback events、週期性 offset 估算，而不是相信 Date.now 或單次 ping。** 不需要 PTP、NTP daemon 或 WebRTC clock sync；那些對 MVP 是過度工程。

具體設計如下：

1. **所有 client 時間一律用 `performance.now()` 相對 session start**
   - `Date.now()` 只可用於人類可讀 log，不可作同步主時間。
   - Client 啟動 session 時記錄：`local_session_start_perf_ms`。
   - 所有事件都用 `client_timestamp_ms = performance.now() - local_session_start_perf_ms`。
   - Server 也使用 monotonic clock 建立 `server_session_start_ms`，所有 `server_timestamp_ms` 都是相對 session start。

2. **ping/pong offset 演算法**
   - Session 開始前，Student 與 Wizard 各自對 Server 做 12 次 sync probe，每次間隔 200–500ms。
   - 每次 probe：
     - Client send time：`t0`。
     - Server receive monotonic：`s1`。
     - Server send monotonic：`s2`。
     - Client receive：`t3`。
   - RTT：`rtt = (t3 - t0) - (s2 - s1)`。
   - Offset 估算：`offset = ((s1 - t0) + (s2 - t3)) / 2`。
   - 丟棄：
     - RTT 最高的 25%。
     - RTT 大於 median RTT + 2 * MAD 的 outlier。
   - 用剩餘樣本的 **median offset**，不要用 average。
   - 如果有效樣本少於 5 個，UI 顯示 sync warning，但不阻塞內部測試；正式 pilot 應重試。

3. **session 中 drift correction**
   - 每 60 秒背景做 5 次 sync probe。
   - 如果新 median offset 與目前 offset 差異小於 30ms，直接更新。
   - 如果差異 30–150ms，做平滑修正：未來 10 秒逐步套用，不回頭改歷史事件。
   - 如果差異超過 150ms，標記 `clock_resync_jump` 事件，並在教師回看端顯示 sync warning。
   - 重連後立即做 6 次 sync probe，完成後才恢復精準 `start_at` 排程。

4. **播放指令要用 future `start_at`，但 clip_started 要以實際播放為準**
   - Server 發 play command 時，設定 `start_at_server_ms = server_now + 500ms`。
   - Student 以 offset 轉換成本地時間，提前 preload，到了時間才 play。
   - 但事件 log 的 `clip_started` 不應等於 command time；Student 必須在 `<video>` 的 `playing` 或第一個可靠 playback callback 後回報實際 `clip_started_actual`。
   - 教師回看時以 actual playback event 對齊，而不是以 Wizard 按鍵時間對齊。

5. **MediaRecorder chunk timing 要記 chunk boundary，而不是只記檔案 sequence**
   - 每個 chunk metadata 要有：`chunk_sequence`、`chunk_start_client_ms`、`chunk_end_client_ms`、`offset_used_ms`、`created_perf_ms`、`upload_ack_server_ms`。
   - FFmpeg remux 後，要把最終錄影 duration 與最後 chunk end time 做 sanity check。
   - 如果差異超過 500ms，標記 `recording_timeline_mismatch`，不要靜默通過。

6. **backup tone + flash sync 要保留，而且建議 session 開始與結束各一次**
   - 開始時：全屏白閃 2–3 frames + 1kHz 短 beep 200ms，寫入 `sync_marker_start`。
   - 結束時：再做一次 flash + beep，寫入 `sync_marker_end`。
   - 這不是即時同步機制，而是外部備用錄影對齊與 drift 檢查工具。
   - 如果擔心干擾學生，可在正式開始前倒數後觸發 start marker；end marker 可在訪談結束後觸發。

### 主要風險與取捨

1. **Browser playback event 不是完美硬體時鐘**：`video.play()` promise resolve、`playing` event、音訊實際出聲之間可能有差距；但對 <300ms 目標已足夠。
2. **MediaRecorder chunk 不保證精準 2000ms**：所以必須記 chunk boundary 與最終 duration sanity check。
3. **LAN jitter 雖小但不是零**：用 12 次 probe、outlier filtering、median 可以避免單次 spike 破壞 offset。
4. **flash/beep 可能被攝影機曝光、自動增益或環境噪音影響**：所以它只能是 backup alignment，不應作為主同步依據。
5. **週期性 drift correction 會增加 protocol 複雜度**：但每 60 秒 5 個小 message 的成本極低，換來教師回看時更可信的 timeline。

### 具體理由

<300ms 的目標不是要做音樂製作級同步，而是讓教師能準確知道學生問題、Wizard 指令、疑犯影片反應與錄影內容的大致相對位置。只要採用 monotonic clocks、median offset、actual playback event、chunk boundary metadata，這個目標在 10–15 分鐘 LAN session 內是合理的。

我反對用單次 ping/pong 或 session 開始時一次 offset 估算後就不管。Chrome、OS scheduling、攝影機/麥克風 pipeline 都可能造成小幅 drift 或 event delay；週期性 correction 很便宜，不做才是冒險。

我也反對把 backup tone+flash 當成主同步方案。它對手機備用錄影很有價值，尤其當主要錄影失敗時可人工對齊；但它無法提供 session 中每個事件的精細 timestamp，也不能替代 WebSocket event timeline。

### 信心程度

**中高。** 我對 <300ms 事件/錄影同步有信心；但若要求「疑犯影片音訊實際出聲」與「學生攝影機錄到的聲音」在所有硬體上都小於 100ms，那就沒有信心。以本規格的教師回看與研究用途，<300ms 是可達成的工程目標。

### 是否有更好的替代方案

如果未來要做到多機位、多房間或高精度同步，可以考慮：

1. WebRTC data channel + RTP timestamp 對齊。
2. PTP/NTP disciplined local clocks。
3. 專用硬體 sync pulse。

但這些都不是初版應該做的事。MVP 最佳替代方案其實是 **雙層同步**：主同步靠 server event timeline + offset correction，備援同步靠 start/end tone+flash + 獨立攝影機。這已經足以支撐 pilot 與教師評核。

---

## 最終總結立場

1. **WebSocket 架構**：採 Server-authoritative session bus、SQLite append-only event log、server sequence、resume snapshot、idempotent resend；不要 Wizard 直控 Student。
2. **Fallback 機制**：固定四類 fallback clip，必須結構化標記 uncovered question；不要即時 LLM/TTS；WebSocket 斷線時 Student 錄影必須本地持續。
3. **時間同步**：用 monotonic clock、多次 ping/pong、outlier filtering、median offset、週期性 drift correction、actual playback event；<300ms 可行，tone+flash 是備援不是主機制。

我的總體建議是：**初版工程資源應優先投在事件可靠性、錄影持續性、fallback 資料化與回看可追溯性，而不是自動生成、更複雜的網絡架構或過度精密的同步技術。**
