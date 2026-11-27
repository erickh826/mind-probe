# mind-probe 開發路線圖（Roadmap）

> 本文件定義里程碑（M0–M8，以及部署軌 D1–D2）、各 Phase 的目標、多 Agent 分工與跨 Agent 協作規則，並記錄目前的進度。
> 與 [`docs/spec.md`](spec.md)（功能規格權威來源）及 [`docs/adr/`](adr/README.md)（架構決策）搭配閱讀——如有衝突，spec.md 與 ADR 的技術決定優先，本文件只定義「依什麼順序做」與「由誰做」。
> 待辦事項及優先次序（P0–P3）見 [`docs/todo.md`](todo.md)。

## 零、目前進度（2026-11-27）

**一句話：** 核心系統（M0–M2）已完成，並已部署到雲端試行（D1）；管理功能（D2）及 CASE001 內容（M3）完成大部分；下一個關卡是 M4 內部 Go/No-go，之後才進入真實學生 mini-pilot（M5）。

- `feat/m2` 是整合分支，最新為 `032e64e`（2026-11-06）。合併進 `feat/m2` 會自動部署四個前端到 Vercel Production；後端（Lightsail）需要手動部署，見 [`deploy/backend/README.md`](../deploy/backend/README.md)。
- 雲端試行環境已有實際試用場次；PR #27 正是根據試用回報而開。
- 未合併：PR #27（伺服器宣布場次開始後才錄影、Leave、IndexedDB 錯誤）、PR #19（有新版前端時提示重新載入）。

### 兩套 Phase 名稱的對照

過去有兩套編號，以下統一使用本文件的名稱：

| 舊稱 | 出處 | 本文件名稱 |
|---|---|---|
| Phase 0–7 | 本文件 | M0–M7（Phase N 對應 M N，不變） |
| 部署設計的「Phase 1：上線與安全」 | [`mindprobe-deploy-design.md`](../mindprobe-deploy-design.md) | **D1 雲端部署與安全** |
| 部署設計的「Phase 2：Admin 管理頁面」（§17） | 同上 | **D2 營運管理功能** |
| spec 的「第二版」（§十六之後） | [`docs/spec.md`](spec.md) | **V2 AI 協助**（M6 之後） |

D1、D2 是與 M1–M3 並行的部署軌，不取代 M4 Go/No-go：部署驗收不等於 pilot 準備就緒。

## 一、里程碑總覽

狀態：✅ 完成　🟡 部分完成／進行中　⬜ 未開始

| 里程碑 | 名稱 | 目標 | 驗收條件摘要 | 狀態 | 主要證據 |
|---|---|---|---|---|---|
| M0 | Scaffold & Contract Lock | monorepo、事件／clip schema、ADR-0001～0004 定案、roadmap | schema 兩端一致；ADR 已接受並寫回 spec | ✅ | `9c4a2ac`、`b46057a`、`edda06d` |
| M1 | 核心即時管線 E2E | 依 ADR-0004 的順序（0001→0003→0002）打通 CASE001 最小流程 | [ADR-0004 M1 通過標準](adr/0004-cross-cutting-rules.md) | ✅ | `5e50dd7`、`6e66e54`、PR #1；`server/tests/test_m1_e2e.py` 逐條對應通過標準 |
| M2 | 錄影與教師回看 MVP | chunk 上傳、FFmpeg、Teacher 回看、時間軸對齊 | 教師端可依統一時間線完整重播一場 session | ✅ | PR #2（完整性、補傳）、#6／#7（review grant）、#8（工作佇列、刪除、儲存） |
| D1 | 雲端部署與安全 | Vercel 前端、Lightsail 後端、R2 儲存；Bearer 認證、端點權限、錄影完整性 | 部署設計 §18 第 1–8 步 | ✅（試行網域） | PR #1、#2、#6–#9；2026-10-27 首次部署；CI #22、Tailscale CD #24。第 9 步（試用期間量度 CPU／RAM）未見紀錄；正式上線前仍要自有網域（第 10 步） |
| D2 | 營運管理功能 | 取代 shell 指令：建場次、派碼、補傳、監察錄影、管理員登入 | 部署設計 §17 | 🟡 | 已完成：OP 管理頁 #13、join code 重發 #14、review code 限速 #18、管理員登入 #20／#21、Wizard 開始／結束 #11／#12。未完成：建立案件及上傳影片、監控頁、直接重新開始的捷徑 |
| M3 | CASE001 內容完整化 | 影片庫、決策樹、揭露規則、rubric 全部串接 | 無缺片；Wizard 2–3 次按鍵內選出任一情境回答 | 🟡 | 30 段影片（在 25–35 上限內）、F1–F8 三層決策樹、9 項 rubric 已有。缺：揭露規則未在伺服器執行、rubric 未有分級描述、按鍵效率未實測 |
| M4 | 內部 Go/No-go 關卡 | 無真實學生的內部乾跑，全流程壓力測試 | 「M4 Go/No-go 門檻表」全部綠燈 | ⬜ 下一步 | 試行場次不等於 Go/No-go；門檻表各項未正式量度 |
| M5 | Mini-pilot（真實學生） | 小規模真人測試，收集操作與計時數據 | 完成 spec 規劃的 mini-pilot（3–5 人，再 15–30 人次） | ⬜ | 需先通過 M4，並備妥研究倫理文件 |
| M6 | Pilot 後迭代 | 依 M5 數據調整影片配額／fallback／rubric | 每個迭代項目對應具體 pilot 發現 | ⬜ | |
| M7 | 多場次／教室規模化 | 多 session 並行、監控、審計完整 | 符合 ADR-0004 審計與保留規定，可支撐多班同時使用 | ⬜ | 現為單機（部署設計 D7） |
| V2 | AI 協助（第二版） | 依 spec：ASR 初稿 → 意圖分類 → 推薦影片 → Wizard 確認 | 進入條件：至少 15–30 次完整 session 及 spec §十六列出的資料 | ⬜ | 不得早於 M5／M6 的數據 |
| M8 | 正式上線／後續擴充 | 正式導入教學現場，規劃第二個案件 | 至少一次完整學期使用無重大事故 | ⬜ | |

> 說明：M0–M7 各對應一個 Phase；D1、D2 為部署軌；V2 與 M8 是 Phase 7 之後的持續階段。

### 下一步的次序

1. 處理試用回報（PR #27）及影響資料可靠性的缺口（老師評分保存、伺服器端揭露規則）。
2. 執行 M4 Go/No-go（門檻表見第三節），同時準備研究倫理文件。
3. 通過後進入 M5 mini-pilot；D2 餘下項目視乎營運需要並行。
4. 正式上線（M8）前完成自有網域等 D1 第 10 步。

詳細項目及優先次序見 [`docs/todo.md`](todo.md)。

## 二、各 Phase 目標、Agent 分工與驗收條件

> 本節保留原定的分工與驗收條件；各 Phase 的實際狀態以第一節為準。D1、D2 的內容見 [`mindprobe-deploy-design.md`](../mindprobe-deploy-design.md) §18、§17。

### Phase 0（對應 M0）— Scaffold & Contract Lock

- **目標**：monorepo 結構、事件封套／clip manifest schema、ADR 流程全部就位並定案。
- **主要負責 Agent**：Docs/ADR Agent 主導；其餘 11 個角色皆須讀過 `docs/spec.md` 與 `docs/adr/` 全文後才進入 Phase 1。
- **交付物**：目前的 repo 結構、`docs/spec.md`、`docs/adr/0001–0004`。
- **驗收條件**：README 的 Status 章節與實際狀態一致；contract 層（`packages/shared` 型別、`server/app/events.py`）沒有遺漏的 TODO stub。

### Phase 1（對應 M1）— 核心即時管線

- **目標**：依 ADR-0004 決定的實作優先順序（**ADR-0001 → ADR-0003 → ADR-0002 → CASE001 M1 E2E**）打通即時互動骨幹。
- **主要負責 Agent**：
  - Server/Realtime Agent — 心跳、指數退避重連、snapshot-first、command 過期判定
  - Shared Types Agent — 事件封套／offset 版本欄位在 TS 與 Python 兩端同步
  - Server/Persistence Agent — session 生命週期與事件持久化
  - QA/Test Agent — 全程配合驗證，最終跑 M1 E2E 測試
- **交付物**：`server/app/websocket.py` 實作、`packages/shared` 事件型別、CASE001 可被 Wizard 端到端控制。
- **驗收條件**：直接採用 [ADR-0004 已定案的 M1 通過標準](adr/0004-cross-cutting-rules.md)（Wizard 可控制 CASE001 影片、Student 穩定播放並錄影、斷線重連後不重播過期命令、技術斷線被正確暫停與記錄、Teacher 端能按統一時間線回看）。

### Phase 2（對應 M2）— 錄影與教師回看 MVP

- **目標**：完成錄影上傳、FFmpeg remux、教師端回看管線。
- **主要負責 Agent**：
  - Server/Media Agent — chunk ingest、FFmpeg remux、資料保留優先順序（依 ADR-0004 並經 ADR-0005 修訂：錄影完整且 MP4 驗證通過後才可刪除本機原始 WebM；incomplete 一律保留）
  - Teacher App Agent — 雙視角回看 UI、時間軸
  - Server/API & Auth Agent — Teacher REST API
- **驗收條件**：完整 session 可被教師端依時間軸重播；所有 chunk 確認上傳成功才允許提示「可安全關閉」。

### Phase 3（對應 M3）— CASE001 內容完整化

- **目標**：把 CASE001 的影片庫、決策樹、揭露規則、rubric 補到符合 spec 配額（25–35 段）。
- **主要負責 Agent**：Case Content Agent 主導；Wizard App Agent 配合把決策樹在 UI 上呈現。
- **驗收條件**：無缺片；Wizard 可在 2–3 次按鍵內選出任一情境的回答。

### Phase 4（對應 M4）— Go/No-go 關卡

- **目標**：在導入真實學生前，完成一次無真人學生的全流程壓力測試。
- **主要負責 Agent**：QA/Test Agent 主導執行；DevOps/Integration Agent 負責跑穩定性壓測；全體 Agent 配合修復阻塞項目。
- **驗收條件**：見下方「M4 Go/No-go 門檻表」，全部項目綠燈才可進入 Phase 5。

### Phase 5（對應 M5）— Mini-pilot

- **目標**：以少量真實學生執行 mini-pilot，收集操作與計時真實數據。
- **主要負責 Agent**：Case Content Agent + Docs/ADR Agent（研究倫理文件、同意書、依 ADR-0002 已定案的 disconnection 揭露分級文案）；QA/Test Agent 現場支援。
- **驗收條件**：完成 spec 規劃的 mini-pilot，資料完整可用於後續配額調整。

### Phase 6（對應 M6）— Pilot 後迭代

- **目標**：依 Phase 5 收集的真實數據調整影片配額、fallback 類別使用比例、rubric 校準。
- **主要負責 Agent**：各角色依數據調整自己負責的模組；Docs/ADR Agent 整理成新的 ADR 或 spec.md 修訂，避免文件與程式再度不同步。
- **驗收條件**：每個調整項目都能追溯到具體的 pilot 發現，並記錄調整前後對比。

### Phase 7（對應 M7）— 規模化準備

- **目標**：支援多 session 並行、教室規模化使用。
- **主要負責 Agent**：DevOps/Integration Agent 主導（多 session 並行部署、監控）；Server/API & Auth Agent 補齊審計與 token 撤銷機制。
- **驗收條件**：符合 ADR-0004 的審計事件與資料保留規定；可支撐多班同時使用而不互相干擾。

## 三、M4 Go/No-go 門檻表

| 項目 | 門檻 | 驗證方式 | 未達標處理 |
|---|---|---|---|
| E2E 穩定性 | CASE001 完整跑 10 次以上，WS 斷線重連無誤播過期命令 | QA 自動化重跑 | 封鎖進入 M5，退回 Phase 1 修正 |
| 時間同步誤差 | 事件記錄誤差低於 300ms（依 spec 驗收標準） | 抽樣 10 場 session 核對 | 檢視 ADR-0003 取樣與 offset 計算邏輯 |
| 錄影完整率 | chunk 上傳成功率 100%，remux 後時長誤差 < 1 秒 | 抽測 20 場 | 排查 `server/app/recording.py` |
| Fallback 使用率 | 同一問題 fallback 比例不超過設計上限（依 ADR-0002 不重複規則） | 統計 decision tree 記錄 | 過高代表 clip manifest 缺片，退回 Phase 3 |
| Wizard 操作效率 | 平均 2–3 次按鍵內完成回答 | 操作時間記錄 | 超標則調整決策樹層數 |
| 審計與資料保留 | 依 ADR-0004，全部審計事件可查、資料保留優先順序正確 | 人工稽核 | 缺漏則 Phase 4 不得通過 |
| 研究倫理準備 | debrief 文件、同意書、disconnection 揭露分級文案齊備（依 ADR-0002） | Docs/ADR Agent 確認 | 缺件不得進入 M5 |

**Go 條件**：以上全部項目達標才可進入 M5。任何一項紅燈即為 No-go，退回對應 Phase 補齊後重新評估。

## 四、建議 Agent 分組（12 個角色）

| # | 角色 | 主要負責目錄 | 核心職責 | 主要依據 |
|---|---|---|---|---|
| 1 | Server/Realtime Agent | `server/app/websocket.py` | 心跳、指數退避重連、snapshot-first、command 過期判定、事件 ACK 去重 | ADR-0001 |
| 2 | Server/Persistence Agent | `server/app/models.py`、`main.py`、`cases.py` | session 生命週期、事件持久化、SQLite schema | spec.md §9 |
| 3 | Server/Media Agent | `server/app/recording.py` | chunk ingest、FFmpeg remux、資料保留優先順序 | spec.md §10、ADR-0004 |
| 4 | Server/API & Auth Agent | `server/app`（REST、auth） | 一次性加入碼／角色綁定 token、審計事件、Teacher REST API | ADR-0001、ADR-0004 |
| 5 | Shared Types Agent | `packages/shared` | 事件封套／clip manifest TS 型別，與 `server/app/events.py` 保持一致 | spec.md §9、§12 |
| 6 | Student App Agent | `apps/student` | 影片播放器、`MediaRecorder` 擷取上傳、斷線 UX | ADR-0001、ADR-0002 |
| 7 | Wizard App Agent | `apps/wizard` | 三層熱鍵控制台、fallback 語意類別選擇、暫停原因選擇 | spec.md §8、ADR-0002、ADR-0004 |
| 8 | Teacher App Agent | `apps/teacher` | 雙視角回看、時間軸、rubric 匯出 | spec.md §10 D |
| 9 | Case Content Agent | `cases/CASE001` | clip manifest、決策樹、揭露規則、rubric 內容撰寫 | spec.md 影片配額章節 |
| 10 | QA/Test Agent | `server/tests`、e2e 腳本 | M1／M4 驗收測試執行、回歸測試 | ADR-0004 M1 通過標準 |
| 11 | DevOps/Integration Agent | `docker-compose.yml`、CI、分支整合 | 跨模組整合、合併衝突解決、環境一致性、穩定性壓測 | 本文件第五、六節 |
| 12 | Docs/ADR Agent | `docs/` | spec.md 同步、ADR 流程主持、roadmap 維護、研究倫理文件 | `docs/adr/README.md` 流程 |

> 小型團隊或單人開發時，一個人可同時扮演多個角色；分組的目的是明確「這件事該對照哪個 ADR／spec 章節、改哪個目錄」，不是強制要求 12 個獨立的人或 12 個獨立的 AI agent。

## 五、多 Agent 協作規則

1. **Contract 先鎖（Contract-first Lock）**：任何跨角色邊界的介面（事件封套 schema、WebSocket 訊息格式、REST API 合約）必須先在 `packages/shared` 與 `docs/spec.md` 中定義並經 Docs/ADR Agent 確認，才能開始實作依賴該介面的程式碼。要修改已鎖定的 contract，走跟架構爭議一樣的流程（提案 → 意見 → 定案 → 寫回 spec.md），不得在功能分支裡直接改動被其他 Agent 依賴的共用型別。
2. **目錄限定（Directory Scoping）**：每個 Agent 只在自己負責的目錄（見第四節表格）內修改程式碼；需要修改其他 Agent 負責目錄的內容時，先在對應目錄下開 issue／PR 說明理由，交由該目錄的負責 Agent 或 Integration Agent 處理，不直接跨目錄改動。
3. **任務驗收條件（Task Acceptance Criteria）**：每個任務／ticket 開工前必須寫清楚可驗證的驗收條件，並明確對應到 `docs/spec.md` 或某份 ADR 的具體段落（例如「依 ADR-0001 第 X 項」）；沒有可驗證條件的任務不得標記為完成。
4. **Integration Agent**：由 DevOps/Integration Agent（角色 11）擔任固定的整合把關者，負責：合併各分支、執行跨模組整合測試、在 Contract 有分歧或 schema drift 時協調並回報 Docs/ADR Agent、在每個 Phase 結束前跑一次全流程穩定性測試。
5. **分支與合併（Branch & Merge）**：實際做法是每批工作開 `agent/<簡述>` 分支（例如 `agent/op-login-form`），以 PR 合併到整合分支 `feat/m2`；每個 PR 先經 reviewer 審查（報告以 `STATUS: PASSED／FAILED` 結尾），由負責人決定 squash-merge。`main` 只在需要時由 `feat/m2` 同步（用 merge commit，不 squash）。**合併進 `feat/m2` 等於部署四個前端**；後端只經手動部署或手動觸發的 CD，順序為前端先行（或同時），並先處理仍開著的舊分頁。

## 六、執行次序圖

```
Phase 0  Docs/ADR Agent
   │  （schema 與 ADR 定案）
   ▼
Phase 1  Server/Realtime + Shared Types  ──▶  ADR-0001（WebSocket）
             │
             ▼
         Server/Realtime               ──▶  ADR-0003（時間同步）
             │
             ▼
         Wizard App + Server/API        ──▶  ADR-0002（Fallback／斷線 UX）
             │
             ▼
         QA/Test Agent                  ──▶  CASE001 M1 E2E（ADR-0004 通過標準）
             │
             ▼
Phase 2  Server/Media + Teacher App     ──▶  錄影與回看 MVP（M2）
             │
             ▼
Phase 3  Case Content + Wizard App      ──▶  CASE001 內容完整化（M3）
             │
             ▼
Phase 4  QA/Test + DevOps/Integration   ──▶  Go/No-go 關卡（M4）
             │
        ┌────┴────┐
      No-go      Go
        │          │
   退回對應Phase   ▼
Phase 5  Case Content + Docs/ADR        ──▶  Mini-pilot（M5）
             │
             ▼
Phase 6  全體 + Docs/ADR                ──▶  Pilot 後迭代（M6）
             │
             ▼
Phase 7  DevOps/Integration + Server/API ──▶  規模化準備（M7）
             │
             ▼
         （持續運作）                    ──▶  正式上線／擴充（M8）
```

> 部署軌 D1（雲端部署與安全）在 Phase 2 期間並行完成；D2（營運管理功能）與 Phase 3、Phase 4 並行。V2 位於 Phase 6 之後，進入條件見第一節。

## 七、範圍界線表

| 角色 | 可直接修改 | 需經 Integration Agent 會簽 | 絕對禁止 |
|---|---|---|---|
| Server/Realtime Agent | `server/app/websocket.py` | `packages/shared` 事件型別變更 | 直接改動 Wizard／Student／Teacher 前端程式碼 |
| Server/Persistence Agent | `server/app/models.py`、`main.py`、`cases.py` | 資料庫 schema 破壞性變更 | 繞過事件封套直接寫入自訂欄位 |
| Server/Media Agent | `server/app/recording.py` | 改動資料保留優先順序邏輯 | 在驗證失敗前清除瀏覽器暫存或原始 chunk |
| Server/API & Auth Agent | REST／auth 相關程式碼 | token 有效期／權限模型變更 | 將 token 寫入 URL query string 或日誌 |
| Shared Types Agent | `packages/shared` | 任何會影響 server 或前端既有呼叫的型別變更 | 單方面修改型別而不同步通知依賴方 |
| Student App Agent | `apps/student` | 影響上傳協定的變更 | 直接呼叫非約定的 server 內部端點 |
| Wizard App Agent | `apps/wizard` | fallback 類別新增／刪除 | 自行改變 Server 端案件狀態解析邏輯 |
| Teacher App Agent | `apps/teacher` | REST API 合約變更 | 直接讀取本地檔案系統繞過 REST API |
| Case Content Agent | `cases/CASE001` | 影片總量配額調整（依修訂 1，25–35 段為硬性上限） | 引入未經審核的疑犯回答內容 |
| QA/Test Agent | `server/tests`、e2e 腳本 | M1／M4 驗收標準的解讀有疑義時 | 在測試不通過時修改驗收條件本身 |
| DevOps/Integration Agent | `docker-compose.yml`、CI 設定 | （本身即為會簽角色） | 跳過驗收條件直接合併到 `feat/m2` 或 `main` |
| Docs/ADR Agent | `docs/` | 任何會改變已定案 ADR 內容的修訂 | 在未經拍板前把「提案中」狀態的 ADR 當作已定案寫入 spec.md |

---

*最後更新：2026-11-27（加入目前進度、D1／D2 部署軌及 V2；原文 2026-07-22）。本文件與 [`docs/adr/README.md`](adr/README.md) 的「實作優先順序」章節保持一致；如兩者有出入，以 ADR-0004 為準並回頭修訂本文件。*
