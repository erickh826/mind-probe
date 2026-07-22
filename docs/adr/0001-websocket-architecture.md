# ADR-0001：WebSocket 架構與斷線重連機制

## 狀態
提案中（Proposed）— 三個模型在核心架構原則上一致，但在心跳頻率、重連時的補送策略、token 設計上有具體分歧，需要人工決定後才能進入 Accepted。

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
- 錄影 chunk 走獨立 HTTP 上傳通道＋IndexedDB 佇列，不與 WebSocket 控制訊息混��（GPT-5.5 明確主張，Claude／Gemini 的設計不衝突，屬於補強共識）。

## 分歧
1. **心跳頻率／斷線判定門檻**：Claude、Gemini 傾向 3 秒 ping（更快偵測斷線），差異在連續失敗次數（3次/9秒 vs 2次/6秒）；GPT-5.5 用 5 秒 app-level heartbeat 但讓 UI 在 6 秒就先警示，判定斷線門檻拉到 15 秒——用「UI 提前警示」與「正式判定斷線」分兩層,避免誤判太敏感。
2. **重連時「盲目重放」vs「先套用 snapshot 再補差異」**：這是本議題中最具體、最會影響實作正確性的分歧。GPT-5.5 明確指出：若重連後只依序重放所有 missed events 而不先套用目前實際狀態的 snapshot，學生端可能在斷線 20 秒後重連時，誤重播一段已經過期的播放指令。Claude、Gemini 的設計偏向直接重放全部遺漏事件，沒有處理「重放到一半可能已經不合時宜」的問題。
3. **Token 設計**：Claude 主張三個角色各自獨立、互不相通的 token；GPT-5.5、Gemini 較接近單一 session secret 派發 role-scoped token；Gemini 允許放 URL query string，GPT-5.5 明確反對（避免被 proxy log 或瀏覽器歷史紀錄意外留存）。

## 建議決定（僅供參考，需你確認）
- 心跳：3 秒 ping／連續 3 次（9 秒）判��斷線，但 UI 在 1 次未回應（3 秒）即顯示「連線不穩」提示——吸收 GPT-5.5「UI 提前於正式判定」的設計，同時維持 Claude/Gemini 較敏感的偵測頻率。
- 重連：採 GPT-5.5 的 snapshot-first 方案（先送目前狀態 snapshot，再補 missed events + idempotent event_id 去重），避免盲目重放造成學生端誤播過期指令。
- Token：採 Claude 的三個獨立 role-scoped token 設計，但傳輸方式採 GPT-5.5 的做法——放在 WebSocket 第一個 auth 訊息內，不使用 URL query string。
- 錄影 chunk 上傳通道維持 spec 原文設計（獨立 HTTP + IndexedDB 佇列），三方無實質衝突。

> 此為整合三方意見後的建議，非最終定案。請確認或修改後，將狀態改為 Accepted，並同步回 `docs/spec.md`。
</content>
