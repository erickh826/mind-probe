# Deployment notes

_Updated: 2026-11-27. The October readiness assessment that used to be here is archived at
[`archive/2026-10-deployment-readiness.md`](archive/2026-10-deployment-readiness.md)._

The deployment itself is documented elsewhere:

| Topic | Where |
| --- | --- |
| Architecture, endpoint permissions, retention, launch order | [`mindprobe-deploy-design.md`](../mindprobe-deploy-design.md) |
| Backend release, upgrade, rollback and CD | [`deploy/backend/README.md`](../deploy/backend/README.md) |
| Frontend (Vercel) settings | [`README.md`](../README.md) → Development → Vercel |
| Recording recovery and upload codes | [`recording-operations.md`](recording-operations.md) |
| Database backup and restore | [`backup-operations.md`](backup-operations.md) |
| Project status | [`roadmap.md`](roadmap.md) |

This page keeps one setting that `.env.example` and `server/app/recording_integrity.py` point to.

## Recording grace on an existing database

`MINDPROBE_UPLOAD_GRACE_SYNTHESIZE_AFTER` decides whether the grace pass may
create a recording row for an ended session that does not have one. `POST
/sessions/{id}/end` always creates that row itself. A websocket `session_ended`
does not. The grace pass used to invent `incomplete` / `grace_expired` plus an
audit for every historical ended session the first time it ran.

This is a deployment choice. The process does not assume an installation date
and does not assume the database is fresh.

| Value | Meaning |
| --- | --- |
| Unset or blank | Existing-database default. Do not invent rows. Sessions that ended before this build keep no recording row and no new audit. |
| `fresh` | Synthesize a row for every ended session that lacks one. Set this only when the operator has confirmed the database was created with this build and has no earlier sessions. |
| ISO-8601 instant (`2026-10-20T00:00:00Z`) | Synthesize only when `ended_at` is at or after that cutover. Earlier sessions are left alone. Use the UTC instant this build is deployed, not a date baked into the source. |
| Anything else | Treated as unset. A warning is logged. No rows are invented. |

Rollout for a database that already has sessions: deploy with the variable
unset, confirm the grace thread is not rewriting old sessions, then set the
variable to the cutover instant and restart if post-cutover websocket ends
must be closed by grace. A brand-new database must set `fresh` explicitly if
that closure is wanted. Leaving it unset is safe; it is not an implicit
`fresh`.
