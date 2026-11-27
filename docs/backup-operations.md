# 備份與還原

_Updated: 2026-11-27_

範圍：正式主機的 **SQLite 資料庫及設定**。錄影另有保留規則，見
[ADR-0005](adr/0005-cloud-deployment-and-recording-retention.md) 及
[部署設計 §13](../mindprobe-deploy-design.md)。

## 兩層備份

| 備份 | 怎樣產生 | 存放地方 | 保留 |
| --- | --- | --- | --- |
| 每日 DB 備份 | 後端 `jobs.py` 每個 UTC 日排一個 `sqlite_backup` 工作：`.backup` 後 gzip，上傳到 `backups/sqlite/<day>/mindprobe.db.gz` | 私有 R2 bucket | bucket lifecycle 30 日 |
| 手動拉回本機 | 維運者在自己的電腦執行 pull-backup 工具：在容器內用 SQLite online backup API 取一致快照，連同部署設定、`MANIFEST.txt`（commit、SHA-256、`integrity_check` 結果）一併拉回 | 維運者電腦，repo 以外 | 由設定 `KEEP` 決定（範例為最近 14 份） |

pull-backup 工具及它拉回的檔案**不放進這個 repo**。拉回的設定檔包括主機的 `runtime.env`，
內有密碼雜湊、token secret 及儲存金鑰，必須存放在 repo 以外、權限 600 的目錄，
不可 commit，也不可貼到聊天或 issue。工具偵測到目的地在 repo 內時會拒絕執行。

不包括：錄影（`/data/sessions/*`、`/data/objects/*`）、Caddy 憑證、案件影片。
案件內容（JSON 及影片）另外備份，同樣不進公開 repo。

## 還原

- 還原步驟在 pull-backup 工具附帶的 `RESTORE.md`。
- 2026-11-05 做過一次本機還原演練（不經 Docker）：以一份實際快照和案件內容備份，在獨立目錄
  跑起 `git archive` 取得的 server 程式，17 項檢查全部通過，資料庫副本用後即刪。
- **未驗證：** 用 Docker 映像還原的路徑（`RESTORE.md` §7b）、從 R2 每日備份還原。

## 注意事項

- 部署設計 §14 建議每季做一次還原演練。
- 正式主機另應開啟 Lightsail 每日快照（部署設計 §14）。
- 腳本一律以 LF 存檔；設定檔出現 CRLF 會令載入失敗。
