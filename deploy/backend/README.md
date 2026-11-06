# 後端部署（Lightsail）與 CI/CD

前端由 Vercel 從 `feat/m2` 自動部署，這裡不涉及。後端一律**手動觸發**：合併 PR 不會重啟後端，以免中斷錄影。

## 流程總覽

| 流程 | 什麼時候跑 | 做什麼 |
|---|---|---|
| `CI`（`.github/workflows/ci.yml`） | 每個 PR、每次 push 到 `feat/m2`／`main` | 後端 pytest（Linux，含 FFmpeg）；`packages/shared` 和四個 app 的測試與 build；部署腳本的語法檢查與模擬 |
| `Backend deploy`（`backend-deploy.yml`） | 在 Actions 頁面按 **Run workflow** | `mode` 預設 **`preflight`**：只做唯讀檢查，什麼都不改。選 `deploy` 並勾選維護時段，才會建立 release 並校驗 → 上傳 → preflight（活動檢查）→ 備份 → 換容器 → 確認版本 → 切換 `current` |
| `Backend rollback`（`backend-rollback.yml`） | 同上 | 回到上次升級前的 release 和 image |

**連線只走 Tailscale：** 每次執行時，GitHub hosted runner 用 OIDC 臨時加入 tailnet，成為 ephemeral 節點，job 結束時登出並移除。runner 透過主機的 Tailscale 內網 IP（`100.x.y.z`）SSH 進去，所以主機的公網 22 可以關掉。GitHub 裡沒有長期有效的 Tailscale 金鑰，也不用 self-hosted runner（repo 是公開的）。

管理員帳密的首次設定與更換（`set-admin.sh`），以及相機錄影、Teacher 播放的驗收，仍由人手動做，CD 不會碰。

## 檔案

| 檔案 | 在哪裡跑 | 作用 |
|---|---|---|
| `make-release.sh` | 本機或 CI | `git archive` 指定 commit，產生 `mindprobe-release.{tar,env,sha256}`；正式部署要求 commit 在 `origin/feat/m2` 上 |
| `ci-deploy.sh` | GitHub runner | 先檢查輸入：模式、commit、維護確認、`DEPLOY_HOST` 是 Tailscale IPv4、`DEPLOY_KNOWN_HOSTS` 有這個位址的主機金鑰。workflow 在加入 tailnet **之前**先只跑這段檢查。接著把 release 和腳本傳到主機的 `/tmp/mindprobe-deploy-<run>/`（0700），以背景方式啟動 `remote-run.sh`，持續讀取 log 直到結束，並回傳主機端的結束碼 |
| `remote-run.sh` | 主機 | 先拿主機鎖（`/opt/mindprobe/.deploy.lock`）並告知子腳本，確認 `sudo -n` 可用，再依模式跑 `preflight.sh`（只讀）、`preflight.sh` + `upgrade.sh`，或 `rollback.sh`。log 寫到 `/opt/mindprobe/deploy-logs/<run>.log`，結束碼寫到 `<run>.exit` |
| `release-info.sh` | 主機 | 校驗 staging 目錄（`MP_TMP`）裡的 release 檔 |
| `preflight.sh`、`activity-check.sh`、`upgrade.sh`、`rollback.sh` | 主機 | 和人手操作時是同一套腳本，詳見下文。`upgrade.sh`、`rollback.sh` 也會自己拿同一把主機鎖 |
| `set-admin.sh` | 主機，**人手執行** | 設定或更換 OP 管理員帳密。CD 會把這份審過的腳本隨部署包上傳到 staging 目錄，但**不會執行**；它也拿同一把主機鎖 |
| `dryrun/` | 本機 WSL 或 CI | 用假的 sudo／docker／curl／ssh／scp 跑完整流程，包括 CD 路徑、唯讀 preflight、Tailscale 位址與主機金鑰檢查。`check-workflows.py` 檢查 workflow 的安全設定：Tailscale action 釘選 SHA、只在 job 層級給 `id-token`、`production` environment、先檢查再加入 tailnet、預設 preflight、CI 不碰 secrets |

**主機鎖：** 所有會改動服務的入口（`remote-run.sh`、直接執行的 `upgrade.sh`、`rollback.sh`、`set-admin.sh`）都用 `/opt/mindprobe/.deploy.lock`。鎖被佔用時，直接執行的腳本回傳結束碼 75，什麼都不改。`remote-run.sh` 持有鎖時會設定 `MP_LOCK_HELD=1`，子腳本會再確認 fd 9 真的是那把鎖才跳過，所以不會自己卡死，單獨設這個變數也繞不過去。`preflight.sh` 和 `activity-check.sh` 只讀，直接執行時不用鎖；經 CD 跑的 preflight 由 `remote-run.sh` 拿鎖，所以不會和部署、回退重疊。

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

## 一次性設定（由負責人做，依這個順序）

公網 22 要等第 8 步的 preflight 經 Tailscale 成功之後才收緊（第 9 步），以免把自己鎖在外面。

1. **主機加入 tailnet：** 在主機上執行（例如用 Lightsail 瀏覽器 SSH）：
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --advertise-tags=tag:mindprobe-prod   # 不要加 --ssh；CD 用的是一般 OpenSSH
   tailscale ip -4                                         # 記下 100.x.y.z
   ```
   - 到 Tailscale 管理頁確認這台機器的 key expiry 已停用。tagged 裝置通常預設就是停用；如果沒有停用，幾個月後主機會掉線。
2. **Tailscale 存取政策（ACL）：** 定義兩個 tag，只允許 CI 節點連到主機的 22：
   ```jsonc
   "tagOwners": {
     "tag:mindprobe-ci":   ["autogroup:admin"],
     "tag:mindprobe-prod": ["autogroup:admin"],
   },
   "grants": [
     {"src": ["tag:mindprobe-ci"], "dst": ["tag:mindprobe-prod"], "ip": ["tcp:22"]},
     // 你自己的裝置要連主機的話，另外加一條，例如 autogroup:member → tag:mindprobe-prod 的 tcp:22
   ],
   ```
   - 預設那條「全部互通」的規則要拿掉，否則 CI 節點能連到 tailnet 裡的每一台機器。
3. **Trust credential（OIDC，沒有長期密鑰）：** Tailscale 管理頁 → Settings → Trust credentials → Credential → OpenID Connect。
   - Issuer：GitHub，即 `https://token.actions.githubusercontent.com`。
   - Subject：`repo:erickh826@174697321/mind-probe@1308329478:environment:production`。
     - 這個 repo 是 2026-07-15 之後建立的，所以 GitHub 用含 owner 和 repo ID 的不可變格式。
     - 前綴可以用 `gh api repos/erickh826/mind-probe/actions/oidc/customization/sub` 確認。
     - 不要用萬用字元。
   - Scope：只勾 **Auth Keys → Write**。Tag 只選 `tag:mindprobe-ci`。
   - 產生後，記下 Client ID 和 Audience。
   - 這個做法沒有要輪換的密鑰：GitHub 每次執行時簽一個短效 token，換成短效的 Tailscale 憑證，加入的節點是 ephemeral，job 結束時登出並移除。
4. **GitHub `production` environment（Settings → Environments）：**
   - **Deployment branches and tags：只允許 `main`。** 不設的話，任何分支上被改過的 workflow 也能拿到符合上面 subject 的 token。
   - 建議加 **Required reviewers**（你自己），每次執行前多一道確認。
5. **部署專用 SSH 金鑰：** 另外產生一把**沒有密碼**的 ed25519 金鑰，不要用個人金鑰。連線用 `BatchMode`，沒辦法輸入密碼。
   - 把公鑰加到主機 `ubuntu` 的 `~/.ssh/authorized_keys`。
   - 這個使用者要能免密碼 `sudo`（Lightsail 的 `ubuntu` 預設可以）。
6. **固定主機金鑰：** 在 Lightsail 瀏覽器 SSH 執行 `cat /etc/ssh/ssh_host_ed25519_key.pub`，取前兩欄，前面加上第 1 步的 Tailscale IP，得到 `100.x.y.z ssh-ed25519 AAAA…`。
   - 主機金鑰本身不變，只是記錄的位址換成內網 IP。
   - 不要在 CI 裡用 `ssh-keyscan` 直接相信。
7. **在 `production` environment 加入 secrets：**

   | Secret | 內容 |
   |---|---|
   | `TS_OAUTH_CLIENT_ID` | 第 3 步的 Client ID |
   | `TS_AUDIENCE` | 第 3 步的 Audience |
   | `DEPLOY_HOST` | 主機的 Tailscale IPv4（`100.x.y.z`）。填公網 IP 或主機名稱會被拒絕 |
   | `DEPLOY_KNOWN_HOSTS` | 第 6 步那一行，開頭的位址要和 `DEPLOY_HOST` 完全相同 |
   | `DEPLOY_SSH_KEY` | 第 5 步的私鑰，整份，包含 BEGIN／END 兩行 |
   | `DEPLOY_USER` | `ubuntu` |

8. **讓按鈕出現，然後先跑 preflight：**
   - `workflow_dispatch` 的 workflow 檔要先存在於預設分支（`main`），Actions 頁面才會出現 Run workflow。
   - 所以這份 PR 合併進 `feat/m2` 後，要用 merge commit 同步 `feat/m2` 到 `main`（不要用 squash）。
   - 接著跑一次 **Backend deploy**：`mode` 保持 `preflight`，填要部署的 commit。這一步什麼都不改。
9. **preflight 經 Tailscale 成功後，才收緊公網 22：** Lightsail → Networking → IPv4 firewall。
   - 把 SSH 規則改成 **Restrict to IP address**，並勾選 **Allow Lightsail browser SSH/RDP**，作為 Tailscale 故障時的備援入口。
   - 不要再留「所有 IP」。
   - IPv6 firewall 也要一起檢查。

## 部署（用 CD）

1. **維護時段：** 通知操作人員不要建立或開始場次。等錄影、補傳和轉檔都結束。
   - 如果這次的前端有改，開著的舊分頁要先關掉或重新載入。
   - **這只是人工確認，不是流量閘門。** 腳本會擋下「已經在進行」的活動，但擋不住新加入的人。
2. **先跑 preflight（建議）：** Actions → **Backend deploy** → Run workflow，`mode` 保持 `preflight`，`commit` 填要部署的 SHA。
   - 它會建立 release、上傳到主機，跑 `preflight.sh`（活動、設定、磁碟、release 校驗），**不建 image、不備份、不換容器、不動 `current`**，也不需要勾維護時段。
   - 結束碼 0 表示這個 commit 現在可以部署。
3. **觸發部署：** 同一個 workflow，`mode` 改選 `deploy`。
   - `commit`：填 `feat/m2` 上的完整 40 字元 SHA。
   - `maintenance_confirmed`：勾選。沒勾的話，在加入 tailnet 之前就停下，什麼都不會做。
4. **看結果：** workflow 會即時顯示主機 log。
   - 結束時，log、`mindprobe-release.env` 和 `.sha256` 會存成 artifact，保留 90 天。
   - 主機上的 `/opt/mindprobe/deploy-logs/` 也保留一份。
   - runner 的 Tailscale 節點在 job 結束時登出並自動移除，不需要手動清理。
5. **冒煙測試**（人工）：
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
| 2，`Tailscale IPv4` 或 `no host key for DEPLOY_HOST` | `DEPLOY_HOST` 不是 Tailscale IP，或 `DEPLOY_KNOWN_HOSTS` 裡沒有這個位址。還沒加入 tailnet，什麼都沒做 | 修正 secret（一次性設定第 6、7 步） |
| `Join the tailnet` 步驟失敗 | OIDC subject、environment 分支限制、tag 或 ACL 不符，或 `ping` 等不到主機 | 對照一次性設定第 1–4 步；在主機上確認 `tailscale status` 顯示在線 |
| ssh `Host key verification failed` | `DEPLOY_KNOWN_HOSTS` 的金鑰和主機實際的不同 | 從 Lightsail 瀏覽器 SSH 重新取得主機金鑰；不要改成不檢查 |
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
