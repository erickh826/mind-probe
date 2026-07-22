# mind-probe 開發路線圖

> 本文件為 mind-probe 的官方開發規劃，列出 Milestone、Phase 分工、Agent 角色分配及多 Agent 協作規則。初版開發範圍以 **M0–M5** 為主。

---

## 一、整體 Milestone 路線

| Milestone | 名稱 | 核心成果 | 性質 |
|---|---|---|---|
| **M0** | Foundation Complete | Repo、Schema、ADR、CI 及三個 App 可重建 | 工程基礎 |
| **M1** | Single-Session E2E | Wizard 控制影片、Student 錄影、Teacher 回看 | 核心閉環 |
| **M2** | Reliability Complete | 重連、ACK、補送、Token、同步及錄影恢復 | 技術可靠性 |
| **M3** | CASE001 Pilot-ready | 完整案件、30 段影片、熱鍵、fallback 及 SOP | 可測試產品 |
| **M4** | Mini-pilot Complete | 完成 3–5 人測試及修訂 | 小規模驗證 |
| **M5** | Formal Pilot Complete | 完成 15–30 人次測試及分析 | 研究驗證 |
| **M6** | AI-assisted Wizard | ASR、意圖分類及影片推薦 | 半自動化 |
| **M7** | Local/Jetson Deployment | 本地模型、投影及離線部署 | 硬體落地 |
| **M8** | Automated Training Platform | 狀態機、自動疑犯及 AI 初評 | 長期版本 |

> **M6 以後不納入初版 backlog。**

---

## 二、Phase 0：工程基礎與介面凍結

### 目標

確保所有 Agent 可以在不互相覆蓋程式碼的情況下開始工作。

### 對應 Milestone

> **M0 — Foundation Complete**

### 可平行 Workstreams

#### Agent A：Architecture／Shared Contracts

負責：統一 Event Envelope、Command Envelope、ACK 格式、Snapshot schema、Session 狀態、Clip Manifest、Fallback 類別、Recording chunk metadata、API／WebSocket 協議文件、更新三份 ADR 狀態。

主要目錄：`packages/shared/`、`docs/`、`cases/CASE001/*.schema.json`

#### Agent B：Backend Foundation

負責：FastAPI 啟動、SQLite models、Case loader、Session 基本 REST API、WebSocket connection manager、健康檢查、Database 初始化。

主要目錄：`server/`

#### Agent C：Frontend Foundation

負責：Monorepo workspace、三個 App 的共同設定、Shared package 引用、統一 WebSocket client、統一 REST client、基本 routing 及錯誤頁面。

主要目錄：`apps/`、`package.json`、`pnpm-workspace.yaml`

#### Agent D：DevOps／QA Foundation

負責：Docker Compose、CI pipeline、`pnpm install`、三個 App build 及 lint、Server pytest、一鍵開發指令、測試資料及環境變數範本。

主要目錄：`.github/workflows/`、`docker-compose.yml`、`Makefile`、`README.md`

### M0 驗收條件

```
✓ 全新 clone 後可一鍵安裝
✓ Student、Wizard、Teacher 全部 build 成功
✓ Shared package typecheck 成功
✓ Server pytest 通過
✓ Docker Compose 可啟動
✓ CASE001 可經 REST API 載入
✓ Event、Command、Snapshot schema 已凍結
✓ ADR-0001 至 0003 改為 Accepted
```

### 關鍵規則

M0 完成前，各功能 Agent 不能自行修改 shared schema。若需要修改，必須由 Architecture Agent 建立新版本，例如：`EventEnvelopeV1`、`SessionSnapshotV1`、`ClipManifestV1`。

---

## 三、Phase 1：建立最小端到端閉環

### 目標

先證明核心資料流成立，不處理完整斷線恢復或正式安全機制。

### 對應 Milestone

> **M1 — Single-Session End-to-End**

### Agent 分工

#### Agent A：Session Backend

建立 session、Student／Wizard／Teacher 加入、Session 基本狀態轉換、Wizard 發送播放要求、Server 驗證 clip、Server 向 Student 下發 `play_clip`、Student 回報 `clip_started` 及 `clip_ended`、保存事件。

#### Agent B：Student Media App

裝置檢查、`getUserMedia()`、`MediaRecorder`、雙影片播放器、Idle → Thinking → Response → Recovery、播放事件上報、全螢幕學生介面。

#### Agent C：Wizard Control App

載入 CASE001、F1–F8 分類、熱鍵選擇、Thinking、Play／Stop／Idle、顯示當前角色及 Session 狀態、未覆蓋問題標記。

#### Agent D：Teacher Playback App

顯示 Session 列表、播放學生錄影、顯示事件時間線、按事件重播疑犯 clip、基本時間跳轉；暫不做完整 rubric。

#### Agent E：Recording Backend

接收 WebM chunk、依 sequence 保存、組合原始 WebM、FFmpeg 轉碼、ffprobe 驗證、錄影狀態 API。

### M1 端到端流程

```
建立 Session
  ↓
Student 及 Wizard 加入
  ↓
裝置檢查
  ↓
開始 Session
  ↓
Student 開始錄影
  ↓
Wizard 選擇 CASE001 回答
  ↓
Server 驗證並下發命令
  ↓
Student 播放影片
  ↓
Student 回報播放事件
  ↓
結束 Session
  ↓
上載及處理錄影
  ↓
Teacher 按時間線回看
```

### M1 驗收條件

```
✓ 單一 CASE001 可正常載入
✓ Wizard 可以播放至少 5 段測試影片
✓ Student 雙播放器沒有明顯黑畫面
✓ Student 可以錄製至少 10 分鐘
✓ 所有 clip 事件可保存
✓ 錄影可轉成 H.264/AAC MP4
✓ Teacher 可按時間線回看
✓ 案件狀態由 Server 控制
```

> M1 暫時可以在理想網絡下驗收。不要因重連、Token 或完整影片庫未完成而阻塞 M1 核心閉環。

---

## 四、Phase 2：可靠性、安全及時間同步

### 目標

將 M1 由「能運行」提升至「資料不容易遺失、斷線不會誤播」。

### 對應 Milestone

> **M2 — Reliability Complete**

### Agent 分工

#### Agent A：WebSocket Reliability

Heartbeat、指數退避重連、Snapshot reconciliation、Command expiry、Command 去重、Event ACK、Event 補送、Sequence gap detection。

#### Agent B：Recording Reliability

IndexedDB chunk 佇列、Chunk checksum、HTTP 上載重試、Chunk ACK、Finalization、`recording_pending_recovery`、刷新後恢復未上載資料、轉碼失敗保留 WebM。

#### Agent C：Authentication／Authorization

一次性加入碼、角色綁定 token、Student／Wizard／Teacher 權限、Token 撤銷、WebSocket 驗證、REST 驗證、Audit log。

#### Agent D：Clock Synchronization

初始 9 次 ping／pong、最低 RTT 五次取 offset 中位數、`start_at` 統一開始、Offset 版本、每 60 秒校正、重連後重新同步、Drift 異常暫停。

#### Agent E：Failure-state UI

三個前端的：重連狀態、技術暫停提示、未上載 chunk 數量、錄影處理狀態、權限過期、恢復／中止流程。

### M2 驗收條件

```
✓ 舊 play 命令重連後不會重新播放
✓ 重複 event 不會重複落庫
✓ Client 刷新後可補送未 ACK event
✓ 未 ACK chunk 可由 IndexedDB 恢復
✓ 錄影 chunk checksum 錯誤會被拒絕
✓ 不同角色不能越權
✓ 不同 Session 不能互相控制
✓ Student、Wizard 時鐘可同步
✓ 技術暫停不計入正式訪談時間
✓ Drift 超過閾值會暫停 Session
```

### 必做故障測試

- 播放影片期間斷開 Wi-Fi；
- 錄影上載期間重新整理；
- 同一 Event 發送兩次；
- 同一 Wizard 登入兩個裝置；
- Student 使用 Wizard token；
- 缺少一個 chunk 後嘗試 finalize；
- FFmpeg 故意失敗；
- WebSocket 重連時存在過期命令。

---

## 五、Phase 3：完整 CASE001 及 Pilot-ready UX

### 目標

將技術平台變成可供真實學生使用的完整實驗。

### 對應 Milestone

> **M3 — CASE001 Pilot-ready**

### Agent 分工

#### Agent A：Case／Content

Ground truth、Suspect knowledge、Disclosure rules、Wizard decision tree、影片文字腳本、Fallback 規則、Rubric、角色狀態。

#### Agent B：Media Production

約 30 段影片、Idle 及 Thinking、案件專屬回答、Reaction、四類 Fallback、Barge-in／Recovery、檔案編碼及響度統一、Clip manifest metadata。

#### Agent C：Wizard UX

F1–F8 熱鍵、三層選擇、預載建議、當前可用回答、禁止狀態下的回答、Fallback 原因標記、連續 Fallback 警告、操作錯誤修正。

#### Agent D：Student Experience

Fullscreen／投影模式、裝置檢查、字幕設定、中性技術暫停、Barge-in、Session 完成提示、自我反思問卷入口。

#### Agent E：Teacher Assessment

雙畫面回看、Clip 及學生影片同步、Wizard 操作紀錄、Fallback 事件、Rubric 輸入、時間碼評語、JSON／CSV 匯出。

#### Agent F：Research／Ethics

參與同意書、Debrief、測試主持 SOP、Wizard SOP、技術故障處理、資料保留、退出及刪除流程、Pilot 問卷。

### M3 驗收條件

```
✓ CASE001 全部內容可由 Server 載入
✓ 約 30 段影片與 manifest 一致
✓ 所有影片首尾狀態可接受
✓ 四類 Fallback 可正常使用
✓ Barge-in 流程可用
✓ Wizard 可在合理時間找到回答
✓ Teacher 可完成 rubric 評分
✓ 同意及 debrief 流程已準備
✓ 完成至少 3 次內部 dry run
```

---

## 六、Phase 4：Mini-pilot 及修訂

### 目標

用少量參與者找出內容、操作及同步問題。

### 對應 Milestone

> **M4 — Mini-pilot Complete**

### 測試規模

```
3–5 名參與者
每次 10–15 分鐘
至少 2 名不同 Wizard／操作員
```

### Agent 分工

**Research Agent**：執行測試、問卷及訪談、整理學生問題、評估自然度、分析是否察覺 Wizard。

**Data Analysis Agent**：計算回答延遲、Clip 使用頻率、Fallback 率、未覆蓋問題、Wizard 選錯率、斷線及技術暫停、同步誤差。

**Bug-fix Agents**（依問題分派）：Backend 可靠性、Student media、Wizard UX、Teacher review、Case／影片內容。

### M4 輸出

```
✓ Mini-pilot 報告
✓ 未覆蓋問題清單
✓ 問題意圖分類初稿
✓ 需要補充／刪除的影片
✓ Wizard 操作時間分布
✓ 技術故障清單
✓ Rubric 修訂建議
✓ M5 是否可進行的 Go／No-go 決定
```

### M4 Go／No-go 門檻

| 指標 | 門檻 |
|---|---:|
| 完整 Session 完成率 | ≥80% |
| 錄影成功保存率 | ≥95% |
| 案件事實矛盾 | 0 |
| 關鍵事件遺失 | 0 |
| 中位回應時間 | ≤4 秒 |
| P90 回應時間 | ≤7 秒 |
| 教師認為資料可評核 | ≥3.5/5 |

---

## 七、Phase 5：正式 Pilot

### 目標

驗證教學價值、內容覆蓋率和評核可用性。

### 對應 Milestone

> **M5 — Formal Pilot Complete**

### 測試規模

```
15–30 人次
一宗案件
一名疑犯
每次 10–15 分鐘
```

### Agent 分工

**Operations Agent**：排期、裝置檢查、Session 建立、Wizard 安排、技術故障處理。

**Research Agent**：同意、Debrief、問卷、使用者訪談、資料匿名化。

**Evaluation Agent**：Rubric 評分、20% 以上雙評分、教師一致性、技術中斷排除、學生反思分析。

**Data Agent**：問題意圖標註、Clip 覆蓋率、Fallback 原因、回應時間、Wizard 行為、故障統計、第二版需求。

### M5 交付

- Pilot 研究報告；
- 最終問題分類；
- 回答覆蓋矩陣；
- Rubric 一致性結果；
- 學生自然度及沉浸感；
- Wizard 工作負荷；
- Phase 6 AI 輔助需求；
- 是否進入 Jetson／投影整合的決策。

---

## 八、Phase 6：AI 輔助 Wizard（M6 後規劃）

### 對應 Milestone

> **M6 — AI-assisted Wizard**

這階段才開始加入 AI，不直接移除 Wizard。

### Agent 分工

**ASR Agent**：廣東話及粵英混合測試、即時逐字稿、關鍵詞保留率、語音分段。

**Intent Agent**：使用 M4／M5 資料建立意圖分類、推薦三個回答、Confidence score、未知意圖檢測。

**Wizard-assist Agent**：將 ASR 與推薦結果加入控制台、Wizard 確認後才播放、記錄推薦是否被採用、計算推薦準確率。

**Evaluation Agent**：比較純人工 Wizard vs AI 輔助 Wizard（回應時間、選錯率、操作負荷、案件一致性）。

---

## 九、Phase 7：Jetson 及投影落地（M7 後規劃）

### 對應 Milestone

> **M7 — Local/Jetson Deployment**

主要工作：API 功能移至本地、本地 ASR、本地意圖分類、本地 LLM 後備、Docker Compose ARM64、移動投影、攝影機及麥克風、自動啟動、離線模式、散熱及連續運作測試、航空箱及教室部署。

> 此 Phase 應由獨立的 Deployment／Edge Agent 負責，不應讓初版 Frontend Agent 同時處理 Jetson 最佳化。

---

## 十、建議 Agent 分組

| Agent | 長期責任 |
|---|---|
| **Architecture Agent** | ADR、schema、API 契約、跨模組設計 |
| **Backend Agent** | FastAPI、Session、案件狀態、資料庫 |
| **Realtime Agent** | WebSocket、ACK、snapshot、同步、重連 |
| **Recording Agent** | MediaRecorder、chunk、IndexedDB、FFmpeg |
| **Student App Agent** | 學生端、播放器、裝置及錄影 UI |
| **Wizard App Agent** | 熱鍵、決策樹、fallback 及控制流程 |
| **Teacher App Agent** | 回看、時間線、rubric 及匯出 |
| **Security Agent** | Token、權限、Audit 及私隱 |
| **QA Agent** | 自動測試、E2E、故障注入及驗收 |
| **DevOps Agent** | Workspace、CI、Docker 及部署 |
| **Content Agent** | CASE001、影片腳本、披露規則 |
| **Research Agent** | Ethics、SOP、Pilot 及結果分析 |

一個 Agent 可以承擔多個相鄰角色，但**不要**讓同一 Agent 同時修改：Shared schema、Backend 實作、所有 Frontend——否則 review 會失去制衡。

---

## 十一、多 Agent 協作規則

### 1. 先鎖 Contract，再平行開發

每個 Phase 開始前先凍結：

```
API endpoints
WebSocket messages
Shared TypeScript types
Pydantic schemas
Session states
Error codes
Acceptance tests
```

Agent 只能按契約實作，不應自行改消息格式。

### 2. 每個 Agent 限定目錄

```
Architecture  → docs/, packages/shared/
Backend       → server/
Student       → apps/student/
Wizard        → apps/wizard/
Teacher       → apps/teacher/
Content       → cases/
DevOps        → root files, .github/
```

若需要跨目錄修改，必須在 PR 中明確列出原因。

### 3. 每個任務必須附驗收條件

不要只寫「實作 WebSocket 重連」，應寫：

```
任務：實作 Student WebSocket 重連

驗收：
1. 斷線後按 0.5/1/2/4/5 秒退避；
2. 重連先接收 snapshot；
3. 不重播過期 command；
4. 未 ACK 事件可以補送；
5. 重複 event 只落庫一次；
6. 通過指定 pytest 及 Playwright 測試。
```

### 4. 每個 Phase 設 Integration Agent

指定一個 Agent 只負責：合併 PR、解決契約差異、執行完整測試、更新 changelog、建立 release tag。

不要讓功能 Agent 自行宣布 Milestone 完成。

### 5. 建議分支

```
main
develop
feature/m1-session-server
feature/m1-student-player
feature/m1-wizard-controls
feature/m1-teacher-review
feature/m2-event-reconciliation
feature/m2-recording-recovery
```

每個 Milestone 完成後建立：

```
v0.1.0-m0
v0.2.0-m1
v0.3.0-m2
v0.4.0-m3
```

---

## 十二、執行次序

```
Phase 0／M0
工程及契約基礎
        ↓
Phase 1／M1
最小端到端流程
        ↓
Phase 2／M2
可靠性、安全、時間同步
        ↓
Phase 3／M3
完整 CASE001 及 Pilot UX
        ↓
Phase 4／M4
3–5 人 Mini-pilot
        ↓
修訂及 Go／No-go
        ↓
Phase 5／M5
15–30 人正式 Pilot
        ↓
Phase 6／M6
AI 輔助 Wizard
        ↓
Phase 7／M7
Jetson 及投影部署
```

## 最重要的範圍界線

| 節點 | 禁止動作 |
|---|---|
| M0 前 | 不做功能美化 |
| M1 前 | 不做 AI |
| M2 前 | 不做完整影片內容 |
| M3 前 | 不進行真實學生測試 |
| M4 前 | 不聲稱系統已具教學效度 |
| M5 前 | 不移除 Wizard |
| M6 前 | 不訓練或微調模型 |
| M7 前 | 不把 Jetson 效能問題混入 Web MVP 驗證 |
