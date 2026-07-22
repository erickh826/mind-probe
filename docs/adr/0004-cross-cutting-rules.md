# ADR-0004：跨 ADR 共同規定

## 狀態
已接受（Accepted）

Decision date: 2026-07-22
Decision owner: GPT-5.6-Sol
Supersedes: none

## 背景
在拍板 ADR-0001（WebSocket 架構）、ADR-0002（Fallback 機制）、ADR-0003（時間同步）時，發現三者之間需要幾條共同約束才能保持一致，因此另立一份 ADR 記錄，而不是分散重複寫進三份文件裡。

## 決議

### 1. Snapshot revision
每次 Server 案件狀態改變：`snapshot_revision += 1`。播放命令必須引用對應 revision，舊 revision 命令不得執行。（用於 ADR-0001 §4–6 的重連協調。）

### 2. Session pause reason
暫停必須記錄原因，取值之一：

```text
manual
network_failure
clock_sync_failure
recording_failure
ethical_or_safety_stop
```

技術暫停期間不進入學生評分指標。（呼應 ADR-0002 §6、§8 的斷線與 fallback 不計分原則。）

### 3. 審計記錄
必須記錄：誰控制了影片、誰觸發 fallback、誰暫停 Session、誰接管連線、誰檢視或下載錄影、Token 何時簽發及撤銷。日誌不得儲存 token 原文。

### 4. 資料保留優先順序
```text
原始事件 > 原始 WebM > 最終 MP4 > 自動生成報告
```
轉碼或報告失敗時不得刪除原始資料。

## 實作優先順序
1. ADR-0001：snapshot、ACK、去重及 token；
2. ADR-0003：時間同步及統一開始時間；
3. ADR-0002：fallback 類別、斷線暫停及倫理提示；
4. 完成 CASE001 端到端 M1 測試。

## M1 通過條件
> Wizard 可控制 CASE001 影片，Student 穩定播放並錄影；斷線重連後不會重播過期命令，事件與錄影 chunk 可以補送；技術斷線會被正確暫停及記錄；Teacher 端能按統一時間線回看。
</content>
