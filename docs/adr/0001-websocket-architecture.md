# ADR-0001：WebSocket 架構與斷線重連機制

## 狀態
已接受（Accepted）

Decision date: 2026-07-22
Decision owner: GPT-5.6-Sol
Supersedes: none

## 背景
依 `docs/spec.md` 第四、五、九、十節與審閱修訂 5、6：Wizard 控制台與學生端各自與 Session Server 建立 WebSocket 連線；規格已要求「WebSocket 斷線後自動重連並補送遺漏事件序號」及「session-level token」，但未定義具體的心跳節奏、重連時的資料補送方式、token 的產生與分發細節。因此請三個模型分別針對這三個子問題給出具體實作建議。

## Agent 意見摘要

| 模型 | Heartbeat 節奏 | 斷線判定門檻 | 重連補送策略 | Token 設計 |
|---|---|---|---|---|
| Claude | 3 秒 ping | 連續 3 次無回應（9 秒） | Server 權威 `event_log`，依 `sequence` 完整依序重放，客戶端去重 | 三個獨立 role-scoped token（wizard／student／observer），Server 產生 opaque UUID |
| GPT-5.5 | 5 秒 app-level heartbeat | 連續 3 次無回應（15 秒）；UI 於 6 秒即顯示「連線不穩」 | 先送 `session_snapshot`（目前實際狀態）再補 `missed_events`，**不盲目重放**，用 `event_id` 做 idempotency | Server 端 opaque token，claims 含 `session_id/role/client_id/issued_at/expires_at`，放在 WebSocket 第一個 auth 訊息，不放 URL query string |
| Gemini | 3 秒 ping | 連續 2 次無回應（6 秒） | 星型拓撲，依 `sequence` 完整 event replay，指數退避重連（500ms 起，上限 5s） | 256-bit 隨機 hex token，可放 URL query 或 auth 封包 |

完整原文：[opinions/claude.md](opinions/claude.md) · [opinions/gpt5.md](opinions/gpt5.md) · [opinions/gemini.md](opinions/gemini.md)

## 共識
- Session Server 是唯一權威狀態來源（星型拓撲），不做 Wizard／學生端 P2P 或 WebRTC 直連。
- 用 `sequence` + Server 端 append-only event log 做重連補送，而非客戶端自行猜測狀態。
- MVP 不需要完整帳號/JWT 系統，opaque token 已足夠。
- 事件需持久化到 SQLite，不能只存在記憶體。
- 錄影 chunk 走獨立 HTTP 上傳通道＋IndexedDB 佇列，不與 WebSocket 控制訊息混雜（GPT-5.5 明確主張，Claude／Gemini 的設計不衝突，屬於補強共識）。

## 分歧
1. **心跳頻率／斷線判定門檻**：Claude、Gemini 傾向 3 秒 ping（更快偵測斷線），差異在連續失敗次數（3次/9秒 vs 2次/6秒）；GPT-5.5 用 5 秒 app-level heartbeat 但讓 UI 在 6 秒就先警示，判定斷線門檻拉到 15 秒——用「UI 提前警示」與「正式判定斷線」分兩層，避免誤判太敏感。
2. **重連時「盲目重放」vs「先套用 snapshot 再補差異」**：這是本議題中最具體、最會影響實作正確性的分歧。GPT-5.5 明確指出：若重連後只依序重放所有 missed events 而不先套用目前實際狀態的 snapshot，學生端可能在斷線 20 秒後重連時，誤重播一段已經過期的播放指令。Claude、Gemini 的設計偏向直接重放全部遺漏事件，沒有處理「重放到一半可能已經不合時宜」的問題。
3. **Token 設計**：Claude 主張三個角色各自獨立、互不相通的 token；GPT-5.5、Gemini 較接近單一 session secret 派發 role-scoped token；Gemini 允許放 URL query string，GPT-5.5 明確反對（避免被 proxy log 或瀏覽器歷史紀錄意外留存）。

## 最終決議（Accepted）

> 以下為人工拍板後的最終決定。與三個模型的原始意見相比，這裡在多處做了取捨（例如心跳頻率改用 10 秒／3 次失敗判定，而非任何一方原始提案），理由見各小節。

### 1. WebSocket 的責任範圍
WebSocket 只負責：Session 狀態通知、Wizard 控制命令、Student 播放器事件、Event ACK、心跳、斷線重連及狀態協調、輕量案件狀態更新。

WebSocket 不負責：錄影 chunk 上載、大型影音檔案傳輸、影片下載、報告匯出——大型資料一律使用 HTTP／REST，避免錄影上載阻塞控制指令。

```text
WebSocket                          HTTP
├── Commands                      ├── Recording chunks
├── Events                        ├── Video files
├── ACK                           ├── Reports
├── Heartbeat                     ├── Case resources
└── State reconciliation          └── Assessment data
```

### 2. 心跳頻率
採用應用層 ping／pong（瀏覽器原生 WebSocket API 無法直接操作協定層 ping frame，因此走 JSON 心跳訊息）：

| 項目 | 數值 |
|---|---:|
| Ping 間隔 | **10 秒** |
| Pong 等候時間 | **5 秒** |
| 連續失敗次數 | **3 次** |
| 判定離線時間 | 約 25–30 秒 |
| 瀏覽器重新進入前景 | 立即 ping 一次 |
| Session 進入 active | 立即 ping 一次 |

```json
{"type": "ping", "ping_id": "01JXYZ", "client_timestamp_ms": 48220}
```
```json
{"type": "pong", "ping_id": "01JXYZ", "server_timestamp_ms": 48510}
```

播放器命令不依賴心跳發現故障；每個播放命令本身仍要有 ACK 及超時。

### 3. 重連策略
指數退避加隨機抖動：`0.5秒 → 1秒 → 2秒 → 4秒 → 5秒 → 5秒……`，每次加入約 ±20% jitter，避免多個 Client 同時重連。

重連期間：Student 端保留未確認事件；IndexedDB 保留未確認錄影 chunk；不自動播放任何新影片；不假設最後一個播放命令仍然有效；顯示或播放的斷線行為見 ADR-0002。

### 4. 重連後：Snapshot 優先（採 GPT-5.5 方向）
**採用**：先取得 Server authoritative snapshot，再補送 Client 尚未被 ACK 的事件。
**禁止**：根據本地最後收到的 sequence，盲目要求 Server 重新傳送全部命令——舊的 `play_clip`／`interrupt_clip`／`return_idle` 可能已過期，盲目重播可能造成學生端重新播放已完成的疑犯回答。

重連流程：
```text
Client 重新建立 WebSocket
        ↓ 驗證 session 及 role token
Client 傳送 reconnect_hello
        ↓
Server 回傳 authoritative snapshot
        ↓
Client 套用 snapshot
        ↓
Client 補送未 ACK events
        ↓
Server 逐項 accepted／duplicate ACK
        ↓
Server 按需要發出新的 reconcile command
        ↓
恢復正常控制
```

```json
{
  "type": "reconnect_hello",
  "session_id": "S001",
  "role": "student",
  "last_snapshot_revision": 18,
  "last_acked_sequence": 46,
  "pending_event_ids": ["01JAAA", "01JAAB"]
}
```
```json
{
  "type": "session_snapshot",
  "snapshot_revision": 19,
  "session_state": "active",
  "playback_state": "idle",
  "active_clip": null,
  "character_state": "defensive",
  "cooperation_level": 2,
  "revealed_facts": ["claimed_leave_time"],
  "server_timestamp_ms": 148510
}
```

### 5. Server 命令不可作永久事件重播
Server 命令分兩類：

- **可持久儲存的業務事件**（進事件記錄，重連時不直接重新執行）：`wizard_selected_fallback`、`clip_started`、`clip_ended`、`clip_interrupted`、`session_paused`、`fact_revealed`
- **短期執行命令**（有效期限，過期即失效）：`play_clip`、`stop_clip`、`return_idle`、`pause_player`

每個短期命令必須包含 `command_id`、`command_type`、`issued_at_ms`、`expires_at_ms`、`snapshot_revision`、`payload`。Student 端必須拒絕：已過期命令、已執行過的 `command_id`、舊 snapshot revision 下發出的命令、Session 狀態不允許執行的命令。

### 6. 重連後的播放器行為
MVP 階段採用安全優先策略：snapshot 顯示 `idle` 就立即進入 Idle；顯示 `paused` 就顯示暫停狀態；斷線前的影片若已過播放有效期不恢復；Server 確認需要繼續時必須發出一個**新的命令 ID**；不從中間自動恢復案件回答影片；不重複播放已開始或已完成的回答。中途恢復機制留待後期若確實需要再開發，初版不做。

### 7. Event ACK 及冪等性
每個事件具備 `event_id`、`session_id`、`source`、`sequence`；資料庫約束 `UNIQUE(event_id)` 與 `UNIQUE(session_id, source, sequence)`。ACK 狀態為 `accepted`／`duplicate`／`rejected`；`accepted` 與 `duplicate` 都代表 Client 可從補送佇列移除；`rejected` 必須附原因（如 `invalid_session_state`）。

### 8. Token 設計（混合 Claude + GPT-5.5 方向）
不使用單一、全角色共用的 session token，改用**一次性加入碼＋短期、角色繫結的 Session Token**：

1. Server 建立 session；
2. 為 Student、Wizard、Teacher 分別產生一次性加入碼；
3. Client 透過 HTTPS 提交加入碼；
4. Server 驗證後簽發角色繫結的短期 token；
5. Token 透過 `HttpOnly`、`Secure`、`SameSite` Cookie 儲存；
6. WebSocket 升級時由 Server 驗證 Cookie；
7. Session 結束後撤銷 token。

```json
{
  "sub": "temporary-client-id",
  "session_id": "S001",
  "role": "wizard",
  "permissions": ["session:read", "clip:control", "event:write"],
  "jti": "TOKEN-01JXYZ",
  "iat": 1784690000,
  "exp": 1784697200
}
```

有效期：預設 **2 小時**，不超過預定 Session 結束後 1 小時；Session 完成或中止後立即撤銷；重連可沿用尚未過期且未撤銷的 token。

明確禁止：Token 放在 WebSocket URL query string；Token 寫入日誌；信任 Client 自行申報的 role；Student token 訪問 Teacher 錄影回看 API；Wizard token 下載學生錄影；長期儲存在 `localStorage`。

### 9. 單一角色連線規則
每個 session：最多一個 active Student 連線、最多一個 active Wizard 連線；Teacher 可以有多個只讀連線；Admin 可以接管但必須產生審計事件。同一角色重複連線時，新連線不能靜默擠走舊連線——Server 通知舊連線，由 Admin 或持有接管許可權的 Client 確認，並記錄 `role_connection_replaced` 事件。

> 跨 ADR 共同規定（snapshot revision、pause reason、審計記錄、資料保留優先順序）見 [0004-cross-cutting-rules.md](0004-cross-cutting-rules.md)。此決定已同步回 `docs/spec.md`。
</content>
