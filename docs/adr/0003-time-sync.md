# ADR-0003：時間同步機制

## 狀態
已接受（Accepted）

Decision date: 2026-07-22
Decision owner: GPT-5.6-Sol
Supersedes: none

## 背景
依 `docs/spec.md` 第九節：Server 與 Client 用多次 ping/pong 估算 clock offset，目標「主要事件與學生錄影之間誤差低於 300ms」，但規格未定義具體取樣次數、統計方法（median/average）、是否需要 session 進行中的持續校正，以及備用 tone+flash 同步法的定位。

## Agent 意見摘要

| 模型 | Ping/Pong 次數 | 統計方法 | 連續 Drift Correction | Tone+Flash 定位 |
|---|---|---|---|---|
| Claude | 7–9 次 | Median，丟棄 RTT 最大 2 筆 | 不做，僅 session 中點（7–8分鐘）背景複測一次作保險，差異>50ms 才記錄警示 | 一次性錨點，只給備用錄影用，非持續同步機制 |
| GPT-5.5 | 12 次 | Median + MAD 離群值過濾（丟 RTT 最高 25%） | 要做，每 60 秒背景 5 次 probe；差異 30–150ms 平滑修正（10秒內套用）；>150ms 標記 `clock_resync_jump` 警示 | 開始＋結束各一次，仍是備援非主機制 |
| Gemini | 5 次 | 剃除最高最低各 1 筆後取**平均值**（明確不用 median） | 明確反對，鎖定初始 offset 直到 session 結束 | 主張應為「強制執行的第一道保險」，非單純備用 |

完整原文：[opinions/claude.md](opinions/claude.md) · [opinions/gpt5.md](opinions/gpt5.md) · [opinions/gemini.md](opinions/gemini.md)

## 共識
- 都同意 <300ms 同步目標在同一區網、10–15 分鐘場景下可行。
- 都同意不需要 NTP daemon、PTP 硬體或 WebRTC clock sync。
- 都同意 tone+flash 對備用攝影機的事後對齊有價值。

## 分歧
1. **Median vs Average**：Claude、GPT-5.5 都主張用 median；Gemini 主張用平均值——這是最有客觀答案的分歧，median 對離群值更穩健是業界共識。
2. **是否做連續 Drift Correction**：GPT-5.5 主張要（每 60 秒背景複測+平滑修正）；Claude 主張中點複測一次作保底；Gemini 主張完全不需要。
3. **Tone+Flash 的角色**：Gemini 主張應強制作為「第一道保險」；Claude、GPT-5.5 較保守，維持備用／事後對齊定位。

## 最終決議（Accepted）

> 以下為人工拍板後的最終決定，統計方法採 GPT-5.5 方向（median 有共識），但取樣次數與 drift correction 頻率做了調整。

### 1. 時間來源
採用兩種時間：

- **Monotonic time**：用於 session 相對時間、事件排序、播放同步、延遲量度、錄影對齊。Client 用 `performance.now()`；Server 用 `time.monotonic_ns()`。
- **UTC wall-clock**：僅用於審計、session 建立日期、日誌、檔案命名、行政記錄。不得用系統 UTC 時間直接計算播放器延遲，因為系統時間可能被調整。

### 2. 初始取樣次數
初始同步使用 **9 次 ping／pong 取樣**——12 次對 10–15 分鐘的區域網 session 沒有明顯必要；5 次又容易受單次排隊延遲影響。每次間隔 100–150ms，總同步時間約 1–2 秒，於裝置檢查階段完成，不計入正式訪談時間。

### 3. Offset 計算
```text
t0 = Client 傳送時間
s1 = Server 接收時間
s2 = Server 傳送時間
t3 = Client 接收時間

RTT = (t3 - t0) - (s2 - s1)
offset = ((s1 - t0) + (s2 - t3)) / 2
```

### 4. Median 還是 Average？（採 GPT-5.5 方向，不採 Gemini 的 average）
採用：**先取 RTT 最低的 5 個樣本，再對 5 個 offset 取中位數**，不採用全部樣本平均值。理由：average 容易受單次網路排隊、瀏覽器 GC 或 CPU 停頓影響；median 對異常值更穩健；最低 RTT 樣本通常最接近網路路徑沒有排隊時的真實 offset。

```text
9 個樣本 → 按 RTT 排序 → 保留最低的 5 個 → 取 offset median
```

### 5. Session 開始方式
Server 不應在收到「開始」請求的瞬間直接將 Session 設為 active，而應發出未來時間：

```json
{"type": "session_start_scheduled", "start_at_server_ms": 155000}
```

建議預留當前 Server monotonic time ＋ 2 秒。Student、Wizard 及錄影模組都根據校正後的時間同時進入 active，比各裝置收到 WebSocket 命令後立即開始更一致。

### 6. 是否連續校正 Drift？（採 Claude 折衷方向，非 GPT-5.5 完整方案）
需要，但不能修改歷史事件：

| 情況 | 校正方式 |
|---|---|
| Session 開始前 | 9 次樣本 |
| Session 進行中 | 每 60 秒進行 5 次輕量取樣 |
| WebSocket 重連後 | 立即進行 5 次取樣 |
| 從 paused 恢復 | 進行 5 次取樣 |
| 瀏覽器由背景回前景 | 進行 5 次取樣 |

連續校正仍使用最低 RTT 的 3 個樣本，取 offset 中位數。

### 7. 不回寫歷史時間
每個事件保存 Client 原始時間、當時使用的 offset、offset 版本、規範化 Server 時間、Server 實際接收時間：

```json
{
  "client_timestamp_ms": 48220,
  "clock_offset_ms": 36,
  "clock_offset_version": 3,
  "normalized_server_timestamp_ms": 48256,
  "server_received_timestamp_ms": 48291
}
```

若後續發現 offset 改變，不能重寫過去事件；新 offset 只用於後續事件——這保留了審計能力，也避免事件時間在處理後「移動」。

### 8. Drift 應用方式
時間校正分兩層：記錄層（新 offset 立即用於未來事件）與 UI／播放器層（不要突然跳動時間軸，offset 變化很小時平滑調整顯示時鐘）。建議閾值：

| Offset 變化 | 處理 |
|---|---|
| ≤25 ms | 直接更新 |
| 26–100 ms | 5–10 秒內平滑調整 |
| 101–300 ms | 記錄警告並重新同步 |
| >300 ms | 暫停 Session 並進入同步異常狀態 |

### 9. 錄影時間對齊
每個錄影 chunk 保存 capture start/end、offset version、normalized server start/end、sequence、SHA-256。最終影片轉碼完成後用 `ffprobe` 取得實際時長並與 chunk 時間線比較，不假設「影片第 0 秒必然等於 Session 第 0 秒」：

```json
{"recording_started_at_session_ms": 1840, "first_decodable_frame_at_session_ms": 1915}
```

教師回看時按這個 offset 對齊。

### 10. 備用物理同步標記（維持備用定位，不採 Gemini 的強制升級）
Pilot 階段保留短促提示聲、畫面閃白、備份攝影機同時錄到——這不是主要同步機制，而是驗證軟體同步、診斷錄影漂移、主要錄影故障後的恢復依據。正式版本是否保留，可在 Pilot 後決定。

> 跨 ADR 共同規定見 [0004-cross-cutting-rules.md](0004-cross-cutting-rules.md)。此決定已同步回 `docs/spec.md`。
</content>
