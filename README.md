# mind-probe

> Wizard-of-Oz 式模擬審訊訓練模擬器（研究型 MVP）
> A Wizard-of-Oz style mock-interrogation training simulator for students.

## 中文概述

mind-probe 是一套供學生練習審訊 / 訪談技巧的訓練系統。初版為**單一案件、單一坐姿疑犯、預生成影片庫、Wizard 人工控制、學生影音錄製、事件同步及教師事後評核**的研究型 MVP。

疑犯以**預生成影片庫**呈現；由 **Wizard** 透過熱鍵決策樹（分類＋熱鍵＋建議佇列）即時挑選回應影片播放給學生；系統同步記錄學生影音、影片播放事件及 Wizard 操作紀錄，供教師事後回看與依 rubric 評分。

初版**不包含**：全自動 AI 疑犯、本地 LLM、即時 AI 生成影片、即時 TTS、即時廣東話逐字稿、AI 正式評分等（詳見 `docs/spec.md` 第三節）。

完整規格（Traditional Chinese，權威來源）：[`docs/spec.md`](docs/spec.md)。

## English Summary

mind-probe is a research-grade MVP training simulator for interview / interrogation skills. A virtual suspect is presented via a **pre-generated video library**. A human **Wizard** drives responses in real time through a hotkey decision tree, while the system records the student's audio/video, clip-playback events, and Wizard actions with synchronized timestamps for later teacher review and rubric scoring.

See the full specification (source of truth) in [`docs/spec.md`](docs/spec.md).

## Architecture

The spec (`docs/spec.md` §4–5) describes a single-LAN setup. The current trial deployment puts the four frontends on Vercel, the server on one Lightsail host behind Caddy, and recordings on Cloudflare R2 (ADR-0005, [`mindprobe-deploy-design.md`](mindprobe-deploy-design.md)). The logical layout is the same:

```
apps/student  (React+TS) ──WS/HTTP──┐
apps/wizard   (React+TS) ──WS───────┼──▶  server/ (FastAPI)  ──▶  SQLite + local FS
apps/teacher  (React+TS) ──REST/WS──┤
apps/op       (React+TS) ──REST─────┘         │  (cases/, sessions/, FFmpeg remux)
                                              └──▶ cases/CASE001/*.json + media/
packages/shared (TS event/clip types, mirrors server/app/events.py)
```

- **server/** — Python FastAPI backend: WebSocket real-time commands, REST API, SQLite persistence, MediaRecorder chunk ingest, FFmpeg remux.
- **apps/student/** — Fullscreen suspect video player, `getUserMedia` + `MediaRecorder` capture/upload.
- **apps/wizard/** — 3-tier hotkey control console (F1–F8 categories, see §8).
- **apps/teacher/** — Dual-view playback + timeline + rubric review.
- **apps/op/** — Operator page: admin login, create sessions, hand out codes, upload codes, recording status.
- **packages/shared/** — Shared TypeScript types for the event envelope (§9) and clip manifest (§12).
- **cases/CASE001/** — Single MVP case: metadata, ground truth, disclosure rules, decision tree, clip manifest, rubric.
- **sessions/** — Runtime recordings / events / Wizard logs (gitignored).

## Repository layout

```
docs/spec.md              # Full finalized spec (Traditional Chinese) — source of truth
server/                   # FastAPI backend (Python)
apps/student/             # Vite + React + TS
apps/wizard/              # Vite + React + TS
apps/teacher/             # Vite + React + TS
apps/op/                  # Vite + React + TS (operator page)
packages/shared/          # Shared TS types
cases/CASE001/            # Example case data
sessions/                 # Runtime data (gitignored)
docker-compose.yml        # server service + frontend dev notes
docker-compose.prod.yml   # production stack (Caddy + server)
deploy/backend/           # backend release, upgrade, rollback and CD kit
```

## Development

### Server

```bash
cd server
uv sync            # or: pip install -e .
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
uv run pytest      # run tests
```

Health check: `GET http://localhost:8000/health`.

### Frontend apps

Each app is an independent Vite project:

```bash
cd apps/student   # or apps/wizard, apps/teacher
npm install
npm run dev        # dev server
npm run build      # production build
```

### Docker

Development (API on port 8000, `MINDPROBE_ENV=development`, no TLS):

```bash
docker compose up server
```

Production-shaped stack for a Lightsail host: Caddy terminates TLS, uvicorn runs one worker with `--proxy-headers`, and only ports 80 and 443 are published. A local smoke test leaves `.env` unset, so storage stays on the local adapter and Caddy serves `https://localhost` with its internal certificate.

```bash
docker compose -f docker-compose.prod.yml up --build
curl -k https://localhost/health
```

Copy `.env.example` to `.env` (mode 600) on the host before a real deploy. `.env` is gitignored.

### Vercel

Each frontend (`apps/student`, `apps/wizard`, `apps/teacher`, `apps/op`) has a `vercel.json` with the SPA rewrite, Content-Security-Policy, and Permissions-Policy from the deploy design. Point the Vercel project root at that app directory and leave `"@mind-probe/shared": "file:../../packages/shared"` unchanged. Set `VITE_API_BASE_URL` and `VITE_MEDIA_BASE_URL` on the project (placeholders are in `.env.example`). The OP app does not read `VITE_MEDIA_BASE_URL`; its production build still fails when `VITE_API_BASE_URL` is unset. The CSP allows the trial hosts `*.sslip.io`, `*.r2.dev`, and `*.r2.cloudflarestorage.com` (path-style presigned URLs). Add the real origin to those directives before moving the API or media onto a custom domain.

Deploying `apps/op` requires a backend that already serves operator-only `GET /sessions` with the `before` / `next_cursor` pagination in [`mindprobe-deploy-design.md`](mindprobe-deploy-design.md) §5. An older backend makes that list fail. Add the OP origin to `MINDPROBE_CORS_ORIGINS`. The Vercel project, CORS allowlist, and `VITE_API_BASE_URL` are the same kinds of settings §11 and §12 already call for. A Teacher Preview that was never given `VITE_API_BASE_URL` is existing Preview configuration, not something the OP page introduces.

## 架構決策記錄（ADR）

多個 AI agent（Claude／GPT-5.5／Gemini）針對關鍵架構問題（WebSocket 斷線重連、Fallback 機制、時間同步）各自獨立提出的意見，以及整合後的分歧點與建議決定，記錄在 [docs/adr/](docs/adr/README.md)。

## 文件索引

| 文件 | 內容 |
|---|---|
| [`docs/spec.md`](docs/spec.md) | 功能規格（權威來源） |
| [`docs/adr/`](docs/adr/README.md) | 架構決策 |
| [`docs/roadmap.md`](docs/roadmap.md) | 里程碑、目前進度、分工與協作規則 |
| [`docs/todo.md`](docs/todo.md) | 待辦（P0–P3） |
| [`mindprobe-deploy-design.md`](mindprobe-deploy-design.md) | 部署設計、端點權限、保留政策 |
| [`deploy/backend/README.md`](deploy/backend/README.md) | 後端發佈、升級、回滾及 CD |
| [`docs/recording-operations.md`](docs/recording-operations.md) | 錄影與補傳操作手冊 |
| [`docs/backup-operations.md`](docs/backup-operations.md) | 備份與還原 |
| [`docs/jobs-storage-delete.md`](docs/jobs-storage-delete.md) | 錄影工作佇列、刪除及儲存合約 |
| [`docs/archive/`](docs/archive/README.md) | 已過時、只作歷史參考的文件 |

## Roadmap

| 里程碑 | 名稱 | 狀態 |
|---|---|---|
| M0 | Scaffold & Contract Lock | ✅ 完成 |
| M1 | 核心即時管線 E2E | ✅ 完成 |
| M2 | 錄影與教師回看 MVP | ✅ 完成 |
| D1 | 雲端部署與安全（原部署設計 Phase 1） | ✅ 試行環境已上線；正式上線前需自有網域 |
| D2 | 營運管理功能（原部署設計 Phase 2） | 🟡 OP 管理頁及管理員登入已完成；建立案件、監控頁未做 |
| M3 | CASE001 內容完整化 | 🟡 影片及決策樹已有；伺服器端揭露規則、rubric 分級描述未做 |
| M4 | 內部 Go/No-go 關卡 | ⬜ 下一步 |
| M5 | Mini-pilot（真實學生） | ⬜ |

M6–M8、V2 及各項的證據見 [`docs/roadmap.md`](docs/roadmap.md)。

## Status

**2026-11-27：雲端試行中，下一個關卡是 M4 內部 Go/No-go。** 詳見 [`docs/roadmap.md`](docs/roadmap.md) 第零節及 [`docs/todo.md`](docs/todo.md)。Implement against `docs/spec.md`.

### Windows 本機執行備註

如 PowerShell 拒絕執行 npm／venv 的腳本，先在該視窗執行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```
