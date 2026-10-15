# ADR-0005：雲端部署、認證合約與錄影保留

## 狀態
已接受（Accepted）

Decision date: 2026-10-15
Decision owner: 專案負責人（設計審閱通過）
Supersedes: ADR-0004 §4（資料保留優先順序，僅限「原始 WebM」一項）；ADR-0001 §8 中「Cookie 儲存 token」的部分（改為 Bearer）
完整設計與測試清單：[`mindprobe-deploy-design.md`](../../mindprobe-deploy-design.md)（v0.5.2，§5 權限表與測試 1–16 為驗收依據）

## 背景
Phase 1 要把系統放上公網：前端在 Vercel、API 在單一 Lightsail 機器、案件影片與錄影在 R2。跨站環境下 cookie 認證不可靠，且現有部分端點沒有認證；錄影也需要可驗證的完整性與可稽核的刪除。這些都改動了已鎖定的合約，依 roadmap §5 先寫 ADR 再實作。

## 決議

### 1. 認證（取代 ADR-0001 §8 的 cookie 傳輸）
- 全面使用 `Authorization: Bearer`；WebSocket 使用 `Sec-WebSocket-Protocol: mindprobe.v1, bearer.<token>`，Server 只回應 `mindprobe.v1`，不得回傳 `bearer.*`。Cookie 只在 `MINDPROBE_AUTH_COOKIE=true`（開發／同源）時讀取。
- Token 類型：`live`（join code 換取，2 小時）、`review`（review code 換取，access 30 分鐘＋可輪替 refresh 7 天）、`operator`（伺服器 env，暫時方案）。review token 在所有 REST／WS 入口一律檢查 grant 與撤銷，**不適用 ended 豁免**；refresh token 被重複使用時撤銷整條鏈。
- 撤銷紀錄帶 `revoke_reason`：`session_ended` 或 `security`。上傳寬限期（預設 600 秒）只放行 `session_ended`，且只開放 chunk、finalize 與 student 的精簡 `GET /recording/{id}/status`。
- 所有端點依設計文件 §5 權限表授權；`POST /sessions`、`POST /sessions/{id}/end`、`/recording/*`、`/cases/*` 不再公開。student 不能讀 `ground_truth`、`wizard_decision_tree`。只有 OP 可建立 session（wizard 不可）。CORS 改為白名單且 `allow_credentials=False`。

### 2. 錄影完整性（補充 spec §10.1）
- 一個 session 只有一段 MediaRecorder 錄影；重新整理後只補傳，不重新開始錄影。
- 序號由 IndexedDB `recording_meta.next_sequence` 在同一 transaction 配發，不因 ACK 回收。
- `POST /recording/{id}/finalize {final_sequence, total_chunks, total_duration_ms, reason?}` 冪等；Server 以完整的 `0..final_sequence`、`total_chunks`、header 有效三項判斷 `complete`，每次收到 chunk 重新檢查。
- 送出 finalize 的門檻：佇列清空即送，否則自 finalize 持久化起最遲 30 秒；此門檻不是上傳逾時。
- `recorder_lost` 或 OP 標記的 incomplete 優先於「chunk 已齊」：不自動轉為 `complete`、不轉檔、不清理。
- 狀態：`recording → finalizing → complete → transcoding → transcoded → uploaded`；另有 `incomplete`、`transcode_failed`、`deleting`、`deleted`。

### 3. 資料保留（取代 ADR-0004 §4 的「原始 WebM」順序）
```text
原始事件 > 最終 MP4（R2） > 原始 WebM／chunks（僅在未完成驗證前） > 自動生成報告
```
- 原始 WebM 不上傳 R2。**只有**狀態為 `complete`（或 OP 確認的 `partial`）且 MP4 通過 `probe_media` 後，才刪除本機 chunks 與 `student_raw.webm`；`incomplete`、`transcode_failed` 永不刪除。刪除前將 chunk 清單、sha256、`final_sequence`、缺漏清單寫入 `recordings.manifest`。
- 本機 MP4 保留並監控磁碟；瀏覽器 `recording_meta` 自 `complete` 確認起保留 7 天，未確認或 incomplete 的資料不因期限被清除。
- 轉碼或報告失敗時不得刪除原始資料（ADR-0004 §4 此句維持有效）。

### 4. 刪除錄影
- 只有 teacher（review token）或 OP 可 `DELETE /sessions/{id}/recording`（body `{"confirm": session_id}`），無 ended 豁免，observer 不可。
- SQLite 交易不得跨越 R2／檔案操作：短交易寫入 `deleting` 與取消標記並建立 `delete_recording` 持久化工作，回 202；清理完成後再以另一短交易寫入 `deleted`。
- 重啟與每小時對帳，對所有 `deleting`／`deleted` session 的固定 key（`sessions/{id}/student.mp4`）重新做冪等刪除。`delete_*` 工作不受取消規則影響、不放棄。
- 審計事件新增：`recording_delete_requested`、`recording_deleted`（沿用 ADR-0004 §3：不記錄 token 原文）。

### 5. 背景工作
SQLite `jobs` 表、單一 worker（`uvicorn --workers 1`）、以子查詢加條件式 `UPDATE … RETURNING` 認領、`worker_instance_id` 啟動即回收舊程序工作、30 秒 heartbeat、5 分鐘過期回收、指數退避。請求內不得同步執行 FFmpeg（`process` 與 `GET /recording` 只建立工作）。

### 6. 儲存
`Storage` 抽象（local／S3 相容）；DB 只存 object key；recordings bucket 私有，teacher 經 15 分鐘 presigned GET 下載，presigned URL 不是認證憑證。案件的 7 個 JSON 只放在伺服器，不上公開 bucket。

## 後果
- 新增／修改的 REST 合約見 `packages/shared/src/api.ts`；Server 的 pydantic 模型須與之一致。
- 正式上線前必須有自有網域；r2.dev 與 sslip.io 只供內部驗證。
- Phase 2 才做 Admin 頁面、`recording_segment_id` 多段錄影與直接重新開始 session。
