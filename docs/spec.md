# mind-probe 初版規格（含審閱修訂）v1.0

> 本文為「初版系統定案與 Review 意見」文件的最終版本。文件開頭的「審閱後修訂」章節列出對原文的修正，其餘為原文全文，作為實作依據。

## 審閱後修訂（優先於下方原文對應章節）

1. **影片數量預算加總超出上限**：原文各類別數量上限相加最多可達 41 段，超過「25–35段」的總體目標。修訂：25–35 段為硬性總量上限，各類別為彈性配額，須互相取捨。第一批建議配額（總數 30）：Idle 2、Thinking 3、Reaction 6、案件專屬 12、Fallback 4、Barge-in 2、程序反應 1，其餘留待 mini-pilot 後依實際數據調整。
2. **事件 JSON schema 前後不一致**：原文第七節 `clip_interrupted` 範例與第九節的標準事件封套（envelope）結構不同（缺少 `session_id`、`sequence`、`server_timestamp_ms`、`clock_offset_ms`、`source`，且欄位未包在 `payload` 內）。修訂：全案統一使用第九節封套格式，所有事件類型專屬欄位（如 `clip_id`、`played_duration_ms`、`reason`）放入 `payload` 物件內。
3. **架構圖資料流向需澄清**：學生端（瀏覽器 React 應用）不能直接寫入伺服器檔案系統。錄影 chunk 一律經 WebSocket/HTTP 上傳至 Session Server，由 Server 負責寫入本地儲存（/cases、/sessions、SQLite）與執行 FFmpeg remux。教師回看端一律透過 Session Server 的 REST API 存取資料，不直接讀取本地檔案系統。
4. **MediaRecorder 錄製格式明確化**：Chrome 的 MediaRecorder 預設輸出 WebM（VP8/VP9 + Opus）。修訂：學生端錄製維持瀏覽器預設 WebM；Session 完成後由 Server 端 FFmpeg 轉封裝/轉碼為 mp4 (H.264/AAC) 供教師回看與長期保存；預生成疑犯影片庫統一用 H.264 baseline profile mp4，確保雙播放器 crossfade 流暢與硬體解碼相容。
5. **WebSocket 斷線與 chunk 上傳重試策略明確化（ADR-0001 已定案）**：心跳採應用層 ping／pong，每 10 秒 ping、5 秒等候 pong、連續 3 次無回應（約 25–30 秒）判定離線；重連採指數退避加隨機抖動（0.5s→1s→2s→4s→5s→5s……，每次 ±20% jitter）。重連後**先取得 Server 權威 snapshot，再補送 Client 尚未 ACK 的事件**（snapshot-first），禁止只憑本地 sequence 盲目要求 Server 全部重播，避免播出已過期的播放指令；短期執行命令（`play_clip`／`stop_clip`／`return_idle`／`pause_player`）需帶 `command_id`、`expires_at_ms`、`snapshot_revision`，過期或舊 revision 命令一律拒絕執行。事件另具 `event_id` 供 ACK 去重（`accepted`／`duplicate`／`rejected`）。chunk 上傳失敗時先寫入 IndexedDB 佇列背景重試，session 結束前必須確認所有 chunk 上傳成功才可提示「可安全關閉」。詳見 [`docs/adr/0001-websocket-architecture.md`](adr/0001-websocket-architecture.md)。
6. **存取控制（ADR-0001 已定案）**：不使用單一、全角色共用的 session token，改用「一次性加入碼＋短期、角色綁定的 Session Token」——Server 為 Student／Wizard／Teacher 各自產生一次性加入碼，Client 以 HTTPS 提交換取角色綁定 token（預設有效期 2 小時，透過 `HttpOnly`／`Secure`／`SameSite` Cookie 儲存，WebSocket 升級時驗證），session 結束後立即撤銷；禁止把 token 放在 WebSocket URL query string 或寫入日誌。每個 session 最多一個 active Student 連線及一個 active Wizard 連線，Teacher 可多個只讀連線，重複連線需經 Admin 確認並記錄審計事件。詳見 [`docs/adr/0001-websocket-architecture.md`](adr/0001-websocket-architecture.md)。
7. **Fallback 選擇機制明確化（ADR-0002 已定案）**：Wizard 按熱鍵選擇的是**語意類別**（`CLARIFY` 要求澄清／`ONE_AT_A_TIME` 要求逐一提問／`UNKNOWN_OR_UNSURE` 不知道或不確定／`DECLINE_OR_BOUNDARY` 拒答或程序界線），Server 再依案件狀態、角色情緒、合作程度、已披露事實解析出實際影片；Server 可在同類別內避免連續重複相同片段，但不得自行改變語意類別。若已有語意貼合、案件狀態允許的案件回答，應正常回答播放，不算 fallback。同一問題最多用一次 fallback；連續兩次 Wizard 端顯示警告，連續三次系統建議主持人暫停。Fallback 本身不自動影響學生評分。詳見 [`docs/adr/0002-fallback-mechanism.md`](adr/0002-fallback-mechanism.md)。
8. **斷線期間學生端顯示、及時間同步演算法明確化（ADR-0002、ADR-0003 已定案）**：WebSocket 斷線 2 秒以內不顯示技術錯誤（可續播 Idle／Thinking，僅背景記錄）；超過 2 秒需顯示中性提示「系統正在重新連線，請稍候。訪談計時已暫停。」並暫停正式計時、禁止 Wizard 發新回答；超過 30 秒進入 `connection_lost`，由主持人決定恢復、重新開始當前問題或中止 session。時間同步初始取樣改為 9 次 ping／pong，取 RTT 最低的 5 個樣本算 offset 中位數（非平均值）；Session 開始改為 Server 發出 `session_start_scheduled`（目前時間＋2 秒）供各端同時進入 active；session 進行中每 60 秒、WebSocket 重連後、暫停恢復後、瀏覽器回前景時各補做 5 次輕量取樣校正，但不回寫已發生事件的時間戳，僅套用於後續事件。詳見 [`docs/adr/0002-fallback-mechanism.md`](adr/0002-fallback-mechanism.md) 與 [`docs/adr/0003-time-sync.md`](adr/0003-time-sync.md)。

9. **跨 ADR 共同規定（ADR-0004 已定案）**：暫停必須記錄原因，取值之一為 `manual`／`network_failure`／`clock_sync_failure`／`recording_failure`／`ethical_or_safety_stop`，技術暫停期間不進入學生評分指標；Server 必須記錄審計事件：誰控制了影片、誰觸發 fallback、誰暫停 session、誰接管連線、誰檢視或下載錄影、token 何時簽發及撤銷，日誌不得儲存 token 原文；資料保留優先順序為「原始事件 > 原始 WebM > 最終 MP4 > 自動生成報告」，轉碼或報告失敗時不得刪除原始資料。實作優先順序：ADR-0001（snapshot、ACK、去重及 token）→ ADR-0003（時間同步）→ ADR-0002（fallback 及斷線暫停）→ CASE001 端到端 M1 測試。M1 通過標準：Wizard 可控制 CASE001 影片、Student 穩定播放並錄影；斷線重連後不會重播過期命令，事件與錄影 chunk 可以補送；技術斷線會被正確暫停及記錄；Teacher 端能按統一時間線回看。詳見 [`docs/adr/0004-cross-cutting-rules.md`](adr/0004-cross-cutting-rules.md)。

以上均為文件層級修正，不影響原文整體方向、範圍界定與時程規劃，不視為阻塞項目。上述第 5–9 項的完整背景（多模型意見比較、共識與分歧、決策理由）保存在 [`docs/adr/`](adr/README.md)，本節為定案後寫回 spec 的摘要版本，如有出入以 ADR 內容為準並回頭修訂本節。

---

# 一、Review 結論

整體方向正確，但初版必須進一步收窄。初版的目的不是證明「全自動 AI 疑犯已經可用」，而是驗證：

1. 學生是否能自然地與虛擬疑犯訪談；
2. Wizard 能否在合理時間內選擇回應；
3. 預製影片能否支援基本自由對話；
4. 學生錄影、疑犯回應及事件能否同步；
5. 案件設計與評分 rubric 是否適用；
6. 收集後續自動化所需的真實問題與操作資料。

因此，正式將初版定義為：

> **單一案件、單一坐姿疑犯、預生成影片庫、Wizard 人工控制、學生影音錄製、事件同步及教師事後評核的研究型 MVP。**

# 二、對 Review 意見的處理決定

| Review 意見 | 決定 | 初版處理方式 |
|---|---|---|
| 初版範圍太大 | 接受 | 刪除影片管理後台、即時AI評分、即時TTS及完整ASR依賴 |
| Wizard 認知負荷過高 | 接受，列為最高優先風險 | 控制台改用「分類＋熱鍵＋建議佇列」，避免主要依賴搜尋 |
| 影片覆蓋率80%過於樂觀 | 接受 | 第一次pilot只量度覆蓋率，不把80%列為硬性驗收要求 |
| 影片切換可能跳接 | 接受 | 所有片段使用統一中性錨點姿勢；雙播放器預載；減少過度crossfade |
| TTS fallback破壞聲音一致性 | 接受 | 初版不使用即時TTS；只使用預製通用修復片段 |
| 廣東話ASR是重大風險 | 接受 | 第一週進行技術測試，但ASR不作為初版核心依賴 |
| 沒有處理學生打斷疑犯 | 接受 | 加入中止影片、Recovery片段及`clip_interrupted`事件 |
| MediaRecorder chunks不能各自播放 | 接受 | 依序保存chunks，完成後用FFmpeg重新封裝；保留首段header和sequence |
| 單機同時錄影和播放可能不穩定 | 部分接受 | 開發期可單機；正式pilot使用學生端和Session Server分開運行 |
| 應加入備用攝影機 | 接受 | Pilot期間使用獨立手機／攝影機作安全備份 |
| 時程應延長至14–18週 | 部分接受 | 技術MVP約8–10週；完整pilot及修訂約12–14週；正式研究約14–16週 |
| 應預錄語音供日後聲音複製 | 延後 | 可取得演員授權及保存語音素材，但初版不開發語音複製 |

# 三、初版範圍

## 3.1 使用情境

| 項目 | 初版設定 |
|---|---|
| 場景 | 固定模擬審訊室 |
| 訪談形式 | 坐姿、一對一 |
| 學生人數 | 每次1名 |
| 疑犯人數 | 1名 |
| 案件數量 | 1宗 |
| 訪談時間 | 10–15分鐘 |
| 操作人員 | 1名Wizard＋1名主持人／觀察員 |
| 顯示方式 | 普通螢幕、電視或現有投影機 |
| 網絡 | 同一區域網絡 |
| AI自動化 | 非必要；可選事後ASR |
| 正式評分 | 教師評分，系統只提供證據及標記 |

## 3.2 初版包含功能

### A. 學生端
- 全螢幕顯示虛擬疑犯；播放Idle、Reaction、Response及Recovery影片；擷取學生攝影機與麥克風；錄製完整學生影音；開始、暫停、恢復及結束session；顯示系統狀態及錄影狀態；可選擇顯示疑犯字幕；接收Wizard的影片播放指令；每次影片播放均產生事件紀錄。

### B. Wizard控制台
- 顯示案件目前階段；以分類查看預設回答；使用鍵盤熱鍵快速選擇回答；預覽或立即播放影片；中止正在播放的影片；觸發「被打斷」反應；返回Idle狀態；選擇角色狀態；標記未覆蓋問題；標記操作錯誤；輸入觀察備註；暫停或結束session。

### C. Session Server
- 建立及關閉session；管理學生端與Wizard端連線；管理案件及影片清單；發送影片播放指令；保存所有事件；管理session時間軸；接收錄影chunks；使用FFmpeg重新封裝錄影；驗證錄影是否完整；提供教師回看資料（透過REST API）；記錄審計事件（依修訂9，包括影片控制、fallback觸發、session暫停、連線接管、錄影檢視或下載、token簽發及撤銷，日誌不存token原文）。

### D. 教師回看端
- 播放學生錄影；根據事件時間線重播疑犯片段；查看Wizard操作紀錄；跳到指定時間；加入教師標記；填寫rubric；匯出JSON、CSV及評分報告。

## 3.3 初版明確不包含
初版不開發：全自動AI疑犯、本地LLM、即時AI生成影片、即時TTS fallback、即時完整廣東話逐字稿、AI正式評分、3D虛擬人物、面部情緒判斷、微表情分析、說謊偵測、人格/緊張/信心推論、多案件、多疑犯、多學生同時使用、VR／AR、LMS整合、複雜影片管理CMS、自動輸出完整雙畫面成品影片。以上功能可以在後續階段加入，但不能阻礙初版完成。

# 四、初版技術架構

同一區域網絡內：Wizard控制台（React+TypeScript）與 Session Server（FastAPI）以 WebSocket 雙向連線；學生端（React+TypeScript）與 Session Server 以 WebSocket/HTTP 連線（錄影chunk經此上傳）。Session Server 統一負責寫入本地儲存（/cases 案件與影片庫、/sessions 錄影/事件/Wizard紀錄、SQLite）並執行 FFmpeg 封裝。教師回看及評分端（雙畫面＋時間線＋rubric）透過 Session Server 的 REST API 讀取本地儲存內容，不直接存取檔案系統。

# 五、技術選型定案

| 元件 | 初版技術 |
|---|---|
| 學生端 / Wizard端 / 教師回看端 | React＋TypeScript（共用前端元件） |
| 後端 | Python FastAPI |
| 即時指令 | WebSocket |
| 一般API | REST API |
| 資料庫 | SQLite |
| 影片及錄影儲存 | 本地檔案系統 |
| 學生影音擷取 | `getUserMedia()` |
| 瀏覽器錄影 | `MediaRecorder`（WebM，Server端轉mp4） |
| 本地暫存 | IndexedDB |
| 影片播放 | HTML5 `<video>`雙播放器（預生成影片庫用H.264 mp4） |
| 影片處理 | FFmpeg |
| 部署 | Docker Compose或直接本地服務 |
| 初版瀏覽器 | Google Chrome固定版本 |
| WebRTC | 非核心；只在Wizard需要遠端監聽時加入 |

FastAPI的選擇主要是為日後加入ASR、分類器及本地LLM保留Python生態系統。

# 六、影片庫初版規模（依修訂1）

總量上限 25–35段，第一批建議配額（總數30）：

| 影片類型 | 建議數量 | 用途 |
|---|---:|---|
| Idle | 2 | 中性聆聽、輕微動作 |
| Thinking／Transition | 3 | 為Wizard選答案爭取自然等待時間 |
| Reaction | 6 | 不耐煩、搖頭、防衛、疑惑 |
| 案件專屬回答 | 12 | 身份、時間線、關係、證據及矛盾 |
| 通用Fallback | 4 | 要求澄清、不明白、不記得、拒答 |
| Barge-in／Recovery | 2 | 學生打斷後的反應 |
| 程序反應 | 1 | 要求休息、律師或終止訪談 |

第一次mini-pilot後，才根據實際學生問題增加影片，總量仍以35為上限直到有數據支持擴充。

# 七、影片播放設計

## 7.1 狀態流程
Idle → 學生完成問題 → Thinking／Transition → Wizard選擇回答 → Response → Recovery → Idle。Thinking片段除了增加自然感，也為Wizard提供約1–3秒選擇答案。

## 7.2 學生打斷疑犯
Response播放中，Wizard按「Interrupted」→ 中止影片及聲音 → 記錄 `clip_interrupted` 事件 → 播放短Reaction／Recovery → 返回Idle。

事件紀錄（依修訂2，使用第九節統一封套格式）：
```json
{
  "session_id": "S001",
  "sequence": 42,
  "client_timestamp_ms": 68420,
  "server_timestamp_ms": 68455,
  "clock_offset_ms": 35,
  "source": "wizard",
  "event_type": "clip_interrupted",
  "payload": {
    "clip_id": "C001_TIMELINE_03",
    "played_duration_ms": 2750,
    "reason": "student_barge_in"
  }
}
```

# 八、Wizard控制台定案

三層結構：第一層案件主題（F1身份/F2人物關係/F3時間線/F4地點/F5證據/F6矛盾/F7程序權利/F8通用回答）；第二層回答選項（例如選「時間線」後顯示1到達時間/2離開時間/3中途行動/4時間不確定/5否認時間/6CCTV矛盾）；第三層語氣狀態（N中性/T思考/D防衛/I不耐煩/R拒絕）。Wizard可在兩至三次按鍵內選出回答。

必備快捷鍵：Space播放/確認、Esc中止影片、I返回Idle、T播放Thinking、B被打斷反應、F Fallback（依修訂7，選擇的是`CLARIFY`／`ONE_AT_A_TIME`／`UNKNOWN_OR_UNSURE`／`DECLINE_OR_BOUNDARY`四個語意類別之一，由Server解析為實際影片）、M標記未覆蓋問題、P暫停session（依修訂9，需選擇暫停原因：`manual`／`network_failure`／`clock_sync_failure`／`recording_failure`／`ethical_or_safety_stop`，技術暫停期間不計入學生評分）、Ctrl+Enter結束session。

# 九、時間同步設計（標準事件封套，依修訂8定案）

Server建立`session_id`；學生端與Wizard端進行9次ping/pong，取RTT最低的5個樣本算offset中位數（非平均值）；Server再發送`session_start_scheduled`（目前monotonic time＋2秒）讓各端同時進入active；session進行中每60秒、重連後、暫停恢復後、瀏覽器回前景時各補做5次輕量取樣校正（同樣取最低RTT 3個樣本的中位數），但不回寫已發生事件的時間戳。所有事件同時保存client相對時間、server接收時間、clock offset、offset版本、sequence number。

事件格式：
```json
{
  "session_id": "S001",
  "sequence": 27,
  "client_timestamp_ms": 48220,
  "server_timestamp_ms": 48510,
  "clock_offset_ms": 36,
  "source": "wizard",
  "event_type": "clip_started",
  "payload": {
    "clip_id": "C001_EVIDENCE_04"
  }
}
```

備用同步：Session開始時同步播放短促提示聲、畫面閃白，備用攝影機同時拍到該畫面及聲音。初版同步目標：主要事件與學生錄影之間誤差低於300ms。

# 十、錄影設計

## 10.1 主要錄影
瀏覽器使用MediaRecorder，每2秒輸出一個chunk（`mediaRecorder.start(2000)`），輸出WebM。每個chunk包含Session ID、Sequence、開始與結束時間、檔案大小、checksum、上載狀態。上傳失敗先進IndexedDB佇列背景重試（依修訂5），session結束前必須全部確認上傳成功。

Server端流程：按sequence保存 → 確認首個header chunk存在 → 訪談完成後合併 → 使用FFmpeg remux（並轉mp4/H.264/AAC，依修訂4）→ 驗證時長及影音軌 → 驗證成功後才清除瀏覽器暫存。資料保留優先順序（依修訂9）：原始事件 > 原始WebM > 最終MP4 > 自動生成報告，轉碼或報告失敗時不得刪除原始資料。

## 10.2 備用錄影
Pilot期間用手機或獨立攝影機固定拍攝學生及房間，Session開始時錄下同步聲，只在主要錄影失敗時使用，不必進入自動分析流程。

# 十一、ASR與AI的初版安排

第一週進行小型ASR技術測試（10–20段廣東話，含粵英混合、快語速、停頓、審訊式問題，比較字錯率及關鍵詞保留率），但無論結果如何，初版不能依賴即時ASR才能運作，Wizard應直接聆聽學生發言。ASR只作為事後逐字稿初稿、教師回看輔助、後續意圖分類資料。

初版不加入即時TTS：遇到未覆蓋問題時只可使用通用澄清片段、拒絕回答片段、標記未覆蓋問題、測試後新增正式片段，避免真人/AI聲音突然改變。

# 十二、初版案件資料

```text
/cases/CASE001/
├── case_metadata.json
├── ground_truth.json
├── suspect_knowledge.json
├── disclosure_rules.json
├── wizard_decision_tree.json
├── clip_manifest.json
├── rubric.json
└── media/
```

Clip manifest例子：
```json
{
  "clip_id": "C001_TIMELINE_LEAVE_02",
  "category": "timeline",
  "intent": "ask_leave_time",
  "character_state": "defensive",
  "text": "我已經說了，我大約九時左右離開。",
  "reveals": ["claimed_leave_time"],
  "requires": [],
  "forbidden_after": ["actual_leave_time_revealed"],
  "duration_ms": 4380,
  "file": "media/C001_TIMELINE_LEAVE_02.mp4",
  "hotkey": "F3-2-D"
}
```

# 十三、初版評核方式

| 評分類別 | 分數 |
|---|---:|
| 開場及程序說明 | 10 |
| 建立關係及尊重 | 10 |
| 提問方式 | 20 |
| 主動聆聽及避免打斷 | 15 |
| 跟進細節及矛盾 | 15 |
| 證據展示策略 | 10 |
| 避免誘導、威嚇及不當承諾 | 10 |
| 總結及結束 | 5 |
| 事後反思 | 5 |
| **總分** | **100** |

建議至少抽取20%錄影，由兩名教師獨立評分，以比較評分一致性。

# 十四、修訂後的驗收標準

## 技術驗收
| 指標 | 初版目標 |
|---|---:|
| Wizard指令至影片開始 | 中位數低於400 ms |
| 學生說完至疑犯開始回應 | 中位數低於4秒 |
| 回應延遲P90 | 低於7秒 |
| 錄影與事件同步誤差 | 低於300 ms |
| 錄影成功保存率 | 95%以上 |
| 關鍵Session事件遺失 | 0次 |
| 案件事實矛盾 | 0次 |
| 10–15分鐘連續運作 | 90%以上session完成 |
| 系統崩潰後可恢復錄影 | 最多損失最後4秒 |

## 研究及內容指標
| 指標 | 初版處理 |
|---|---|
| 預設影片覆蓋率 | 第一次pilot只量度，不設硬性門檻 |
| 修訂後覆蓋率 | 目標70%以上 |
| Fallback率 | 記錄並按意圖分類 |
| Wizard選錯影片 | 初期低於10%，修訂後低於5% |
| 學生自然度評分 | 平均3.5/5以上 |
| 教師認為資料可供評核 | 平均4/5以上 |
| 未預期問題 | 全部保存並標注意圖 |
| 學生完成訪談比例 | 90%以上 |

# 十五、初版開發時程

## 技術MVP：8–10週
| 週數 | 工作 |
|---|---|
| 第1週 | 確定案件、rubric、研究倫理及ASR測試 |
| 第2–3週 | 建立學生端、Wizard端、Server及事件協議 |
| 第3–5週 | 並行製作第一批影片及Wizard決策樹 |
| 第4–6週 | 雙播放器、預載、熱鍵及barge-in處理 |
| 第6–7週 | MediaRecorder、chunk上載及FFmpeg remux |
| 第7–8週 | 教師基本回看介面及資料匯出 |
| 第9週 | 內部alpha測試 |
| 第10週 | 3–5人mini-pilot |

## Pilot-ready版本：12–14週
| 週數 | 工作 |
|---|---|
| 第11週 | 根據mini-pilot補充回答及修正延遲 |
| 第12週 | Wizard SOP、主持人SOP及備份流程 |
| 第13–14週 | 進行15–30人次測試 |

如果只有一名兼職開發者，建議把完整pilot時程預留至14–16週。

# 十六、初版交付成果

## 軟體
學生端Web應用、Wizard熱鍵控制台、Session Server、事件紀錄系統、學生錄影及恢復機制、雙影片播放器、基本教師回看介面、Rubric輸入頁面、JSON／CSV匯出、本地部署文件。

## 內容
一宗完整案件、一名疑犯設定、案件真相表、疑犯知識表、披露規則、Wizard決策樹、約25–35段影片、Wizard操作SOP、主持人測試SOP、同意書及debrief文件、評分rubric、Pilot問卷。

# 十七、初版後的擴展門檻

完成Pilot後不應立即全面自動化，只有取得至少15–30次完整session、學生問題意圖分類表、未覆蓋問題清單、各類問題出現頻率、Wizard平均回應時間、Wizard選錯影片原因、各影片使用次數、Fallback使用原因、教師評分一致性、學生對自然度及沉浸感的評價、廣東話ASR測試結果後，才進入第二版。

第二版優先功能：學生問題 → ASR初稿 → 意圖分類 → 推薦三個影片 → Wizard確認 → 播放。第二版仍保留Wizard，只由AI協助搜尋及推薦。

# 最終定案

初版正式採用：以預生成影片呈現疑犯，由Wizard透過熱鍵決策樹控制回應，系統同步記錄學生影音、影片事件及操作紀錄，再由教師事後回看和評分。

初版最重要的三個開發項目依次是：
1. Wizard能否快速、穩定及一致地控制疑犯；
2. 學生錄影與疑犯事件能否可靠同步及保存；
3. 案件、問題和Wizard操作能否形成後續自動化所需的結構化資料。

AI生成、Jetson部署、投影整合、ASR、LLM及自動評分全部屬於後續擴展，不應成為初版上線的阻塞條件。
</content>
