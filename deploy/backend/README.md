# 後端部署（Lightsail）與 CI/CD

前端由 Vercel 從 `feat/m2` 自動部署，這裡不涉及。後端一律**手動觸發**：合併 PR 不會重啟後端，以免中斷錄影。

## 流程總覽

| 流程 | 什麼時候跑 | 做什麼 |
|---|---|---|
| `CI`（`.github/workflows/ci.yml`） | 每個 PR、每次 push 到 `feat/m2`／`main` | 後端 pytest（Linux，含 FFmpeg）；`packages/shared` 和四個 app 的測試與 build；部署腳本的語法檢查與模擬 |
| `Backend deploy`（`backend-deploy.yml`） | 在 Actions 頁面按 **Run workflow** | 建立 release 並校驗 → 上傳 → preflight（活動檢查）→ 備份 → 換容器 → 健康檢查與路由比對 → 切換 `current` |
| `Backend rollback`（`backend-rollback.yml`） | 同上 | 回到上次升級前的 release 和 image |

管理員帳密的首次設定與更換（`set-admin.sh`），以及相機錄影、Teacher 播放的驗收，仍由人手動做，CD 不會碰。

## 檔案

| 檔案 | 在哪裡跑 | 作用 |
|---|---|---|
| `make-release.sh` | 本機或 CI | `git archive` 指定 commit，產生 `mindprobe-release.{tar,env,sha256}`；正式部署要求 commit 在 `origin/feat/m2` 上 |
| `ci-deploy.sh` | GitHub runner | 把 release 和腳本傳到主機的 `/tmp/mindprobe-deploy-<run>/`（0700），以背景方式啟動 `remote-run.sh`，持續讀取 log 直到結束，並回傳主機端的結束碼 |
| `remote-run.sh` | 主機 | 先拿主機鎖（`/opt/mindprobe/.deploy.lock`）並告知子腳本，確認 `sudo -n` 可用，再跑 `preflight.sh` + `upgrade.sh`，或 `rollback.sh`。log 寫到 `/opt/mindprobe/deploy-logs/<run>.log`，結束碼寫到 `<run>.exit` |
| `release-info.sh` | 主機 | 校驗 staging 目錄（`MP_TMP`）裡的 release 檔 |
| `preflight.sh`、`activity-check.sh`、`upgrade.sh`、`rollback.sh` | 主機 | 和人手操作時是同一套腳本，詳見下文。`upgrade.sh`、`rollback.sh` 也會自己拿同一把主機鎖 |
| `set-admin.sh` | 主機，**人手執行** | 設定或更換 OP 管理員帳密。CD 會把這份審過的腳本隨部署包上傳到 staging 目錄，但**不會執行**；它也拿同一把主機鎖 |
| `dryrun/` | 本機 WSL 或 CI | 用假的 sudo／docker／curl／ssh／scp 跑完整流程，包括 CD 路徑 |

**主機鎖：** 所有會改動服務的入口（`remote-run.sh`、直接執行的 `upgrade.sh`、`rollback.sh`、`set-admin.sh`）都用 `/opt/mindprobe/.deploy.lock`。鎖被佔用時，直接執行的腳本回傳結束碼 75，什麼都不改。`remote-run.sh` 持有鎖時會設定 `MP_LOCK_HELD=1`，子腳本會再確認 fd 9 真的是那把鎖才跳過，所以不會自己卡死，單獨設這個變數也繞不過去。`preflight.sh` 和 `activity-check.sh` 只讀，不用鎖。

`upgrade.sh` 的 7 個階段：
1. 校驗 release，確認正在跑的 stack 來自 `current`，記下回退資訊。
2. 把正在跑的 image 標為 `pre-<短碼>`。
3. 解開 release 到 `releases/<短碼>`，並把 commit 寫進 `server/RELEASE_COMMIT`（build context，會進到 image 的 `/app/RELEASE_COMMIT`）。
4. 建新 image，期間舊容器繼續服務。
5. **再查一次活動**，然後備份 SQLite。
6. 換新容器，等健康檢查通過。
7. 確認 release 身分，最後才切換 `current`：
   - 容器來自新的 release 目錄，而且 image 內的 `/app/RELEASE_COMMIT` 等於這次的 commit。
   - 對公開 HTTPS 網址送一個唯一的探針請求（`/health?mp_release_probe=<隨機碼>`），這個碼必須出現在**新容器**自己的 access log 裡；公開端點若還指向舊版，就看不到。
   - 補充檢查：公開 `/openapi.json` 的路由和新容器自己列出的路由相同。路由相同**不能**證明版本正確，只是多一道檢查。

## 一次性設定（由負責人做）

1. **部署專用 SSH 金鑰：** 建議另外產生一把，不要用個人金鑰。把公鑰加到主機 `ubuntu` 的 `~/.ssh/authorized_keys`。這個使用者要能免密碼 `sudo`（Lightsail 的 `ubuntu` 預設可以）。
2. **固定主機金鑰：** 從可信的管道取得主機的 host key，例如 Lightsail 主控台的瀏覽器 SSH，或你已確認過的 `~/.ssh/known_hosts` 那一行。不要在 CI 裡用 `ssh-keyscan` 直接相信。
3. **在 GitHub → Settings → Secrets and variables → Actions 加入：**

   | Secret | 內容 |
   |---|---|
   | `DEPLOY_SSH_KEY` | 部署私鑰（整份） |
   | `DEPLOY_KNOWN_HOSTS` | 主機金鑰那一行（`<host> ssh-ed25519 AAAA…`），host 要和 `DEPLOY_HOST` 寫法一致 |
   | `DEPLOY_HOST` | 主機位址，例如 `95.41.69.24` |
   | `DEPLOY_USER` | `ubuntu` |

   workflow 使用名為 `production` 的 Environment。上面四個 secret 放在 repository 層或 `production` environment 都可以。如果方案支援，可以在 `production` 加上 required reviewers，作為第二道確認。
4. **讓按鈕出現：** `workflow_dispatch` 的 workflow 檔要先存在於預設分支（`main`），Actions 頁面才會出現 Run workflow。所以要等這份 PR 合併進 `feat/m2`，再用 merge commit 同步 `feat/m2` 到 `main`（不要用 squash）。

## 部署（用 CD）

1. **維護時段：** 通知操作人員不要建立或開始場次。等錄影、補傳和轉檔都結束。
   - 如果這次的前端有改，開著的舊分頁要先關掉或重新載入。
   - **這只是人工確認，不是流量閘門。** 腳本會擋下「已經在進行」的活動，但擋不住新加入的人。
2. **觸發：** Actions → **Backend deploy** → Run workflow。
   - `commit`：填 `feat/m2` 上的完整 40 字元 SHA。
   - `maintenance_confirmed`：勾選。沒勾的話，什麼都不會做。
3. **看結果：** workflow 會即時顯示主機 log。
   - 結束時，log、`mindprobe-release.env` 和 `.sha256` 會存成 artifact，保留 90 天。
   - 主機上的 `/opt/mindprobe/deploy-logs/` 也保留一份。
4. **冒煙測試**（人工）：
   - OP 列表正常，可以建立測試場次。
   - Student 錄一段短影片，Stop REC 後顯示可以安全關閉，OP 詳情顯示 complete。
   - Teacher 用 review code 登入並播放。

**同時只會有一個部署：** 部署和回退共用 `backend-production` 這個 concurrency group，第二個會排隊，不會被取消。主機上所有會改動服務的入口（CD、直接執行的 `upgrade.sh`／`rollback.sh`、`set-admin.sh`）共用同一把 `flock`，所以手動操作和 CD 不會同時執行；遇到時回傳結束碼 75。

**SSH 中途斷線：** 主機上的流程是用 `setsid nohup` 在背景跑的，不受影響。runner 那邊最多重試輪詢 30 次。如果 workflow 逾時或失聯，到主機查 `deploy-logs/<run>.log` 和 `<run>.exit`。

**校驗碼：** release 由 commit 決定。tar 的位元組會隨 git 版本不同（Windows 和 Linux 的 git 打出來就不一樣），所以 CD 產生的 sha256 不會等於人工審查時那份。要比對的是 summary 裡的 commit 和 tree。

## 結束碼與失敗後怎麼做

| 結束碼／訊息 | 意思 | 怎麼做 |
|---|---|---|
| 2，`Not confirmed` 或 `40-character` | 輸入不對，什麼都沒做 | 修正輸入後重跑 |
| 1，`Activity: WAIT`（preflight 或第 5 階段） | 有進行中的場次、上傳或轉檔，舊後端照常服務 | 等活動結束後重跑 |
| 75 | 另一個部署、回退或管理員設定正在主機上跑（CD 或手動都一樣） | 等它結束 |
| 1，`passwordless sudo` | 部署帳號的 sudo 需要密碼 | 修正主機設定 |
| 1，在第 1–5 階段失敗 | **舊後端仍在服務**，`current` 沒動 | 看 log 找原因，修正後重跑同一個 commit；解開過的 release 只有內容一致時才會沿用 |
| 1，在第 6–7 階段失敗（`Run rollback.sh`） | 新容器**可能**已在跑，`current` 仍指舊版 | 先看實際狀態，再執行 **Backend rollback**，確認恢復後才重試 |
| 124 或 `Lost the host` | runner 等不到結果 | 主機上的流程可能還在跑，先查主機 log，不要立刻重跑 |

## 回退

- **用 CD：** Actions → **Backend rollback**，勾選 `maintenance_confirmed`。
- **手動：** `bash rollback.sh`（主機上任何一份 kit 都可以），或 `remote-run.sh rollback <id>`。

回退會用 `/opt/mindprobe/rollback-target`（目錄）和 `rollback-image`（`pre-<短碼>`），這兩個檔案在上一次 upgrade 時寫入。資料庫不會還原。每次版本改到資料表或審計紀錄時，要先確認舊程式讀得了新資料。444edcc 的確認見 `tasks/02_implement/deploy-pr18-pr20/verify/rollback_probe.py`。

## 手動部署（不經 CD，備援用）

把 `make-release.sh` 產生的三個 release 檔，加上 `release-info.sh`、`preflight.sh`、`activity-check.sh`、`upgrade.sh`、`rollback.sh`、`set-admin.sh` 放到主機的同一個目錄，然後：

```bash
export MP_TMP=<那個目錄>
bash $MP_TMP/preflight.sh && bash $MP_TMP/upgrade.sh --maintenance-confirmed
```

直接執行 `upgrade.sh`／`rollback.sh` 時，如果 CD 或另一個操作正在跑，會回傳 75 並且什麼都不改。

## 管理員帳密（人手）

第一次啟用 OP 登入，或要更換密碼時，在主機上執行**這次部署上傳的那一份** `set-admin.sh`。CD 部署成功後，log 結尾會印出完整路徑，位置是 `/tmp/mindprobe-deploy-<run id>/set-admin.sh`（`<run id>` 是 Actions 的 run id 和 attempt）：

```bash
bash /tmp/mindprobe-deploy-<run id>/set-admin.sh
```

不要用主機上其他地方留下來的舊版。這份是從審過的 kit 上傳的，即使這次部署的 commit 早於 `deploy/backend`（tar 裡沒有 `set-admin.sh`）也一樣會有。帳密一律由你在提示時手動輸入，CD 不會執行它，也不會經手任何帳密。
- 帳號輸入時會顯示，密碼不會；密碼要 12–1024 字元，輸入兩次。
- 它會先備份 `runtime.env`，只替換管理員那兩行，重啟 server，然後實際登入一次再登出。

**一般部署不會動 `runtime.env`。**

## 本機模擬

```bash
bash deploy/backend/dryrun/run.sh                 # Linux：從 HEAD 產生 release
bash deploy/backend/dryrun/run.sh <release 目錄>  # WSL 跑 Windows 上的 worktree 時，先用 Git Bash 執行 make-release.sh
```
