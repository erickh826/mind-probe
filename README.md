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

Everything runs on a single LAN (see `docs/spec.md` §4–5):

```
apps/student  (React+TS) ──WS/HTTP──┐
apps/wizard   (React+TS) ──WS───────┼──▶  server/ (FastAPI)  ──▶  SQLite + local FS
apps/teacher  (React+TS) ──REST─────┘         │  (cases/, sessions/, FFmpeg remux)
                                              └──▶ cases/CASE001/*.json + media/
packages/shared (TS event/clip types, mirrors server/app/events.py)
```

- **server/** — Python FastAPI backend: WebSocket real-time commands, REST API, SQLite persistence, MediaRecorder chunk ingest, FFmpeg remux.
- **apps/student/** — Fullscreen suspect video player, `getUserMedia` + `MediaRecorder` capture/upload.
- **apps/wizard/** — 3-tier hotkey control console (F1–F8 categories, see §8).
- **apps/teacher/** — Dual-view playback + timeline + rubric review.
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
packages/shared/          # Shared TS types
cases/CASE001/            # Example case data
sessions/                 # Runtime data (gitignored)
docker-compose.yml        # server service + frontend dev notes
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

```bash
docker compose up server
```

The frontend apps are run with Vite dev servers during development (see `docker-compose.yml` notes).

## 架構決策記錄（ADR）

多個 AI agent（Claude／GPT-5.5／Gemini）針對關鍵架構問題（WebSocket 斷線重連、Fallback 機制、時間同步）各自獨立提出的意見，以及整合後的分歧點與建議決定，記錄在 [docs/adr/](docs/adr/README.md)。

## Roadmap

完整的 M0–M8 里程碑、Phase 0–7 開發階段、建議的 12 個 Agent 分工與協作規則，詳見 [`docs/roadmap.md`](docs/roadmap.md)。

| 里程碑 | 名稱 | 狀態 |
|---|---|---|
| M0 | Scaffold & Contract Lock | 進行中 |
| M1 | 核心即時管線 E2E（依 ADR-0004：0001→0003→0002） | 未開始 |
| M2 | 錄影與教師回看 MVP | 未開始 |
| M3 | CASE001 內容完整化 | 未開始 |
| M4 | 內部 Go/No-go 關卡 | 未開始 |
| M5 | Mini-pilot（真實學生） | 未開始 |

完整 M0–M8 與後續 Phase 詳情見 [`docs/roadmap.md`](docs/roadmap.md)。

## Status

**當前里程碑：M0（Scaffold & Contract Lock）—— 進行中。**

file structure, schemas, and skeletons are in place；ADR-0001～0004 已定案；business logic 仍為 stub，尚未開始 M1 實作。

M0 驗收條件：
- [x] repo 結構符合本 README 所描述的 monorepo 佈局
- [x] `packages/shared` 與 `server/app/events.py` 事件封套 schema 一致
- [x] ADR-0001～0004 狀態均為「已接受」，且已寫回 `docs/spec.md`
- [x] `docs/roadmap.md` 建立，並與本 README、`docs/adr/README.md` 保持同步
- [ ] 進入 Phase 1，開始實作 ADR-0001（WebSocket）

完整里程碑定義、Agent 分工與驗收標準見 [`docs/roadmap.md`](docs/roadmap.md)。Implement against `docs/spec.md`.

local run 
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) 