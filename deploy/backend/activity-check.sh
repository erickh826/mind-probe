#!/usr/bin/env bash
# Read-only activity check against the running backend. Exit 0: nothing in
# progress. Exit 1: wait. Called by preflight.sh and again by upgrade.sh right
# before the backup and cutover. Prints no secret values.
set -euo pipefail
compose=(sudo docker compose -p mindprobe-prod -f docker-compose.prod.yml)
cd "$(readlink -f "${MP_ROOT:-/opt/mindprobe}/current")"

# Activity, read-only, inside the running server. Chunk uploads do not touch
# the recordings row, so recent uploads come from GET /sessions'
# seconds_since_last_chunk (chunk file times), the same fact the operations
# doc has OP compare. The operator token is read from the container env and
# never printed.
"${compose[@]}" exec -T server python - <<'PY'
import json, os, sqlite3, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

rows, before = [], None
for _ in range(50):
    query = {'limit': 200, **({'before': before} if before else {})}
    request = urllib.request.Request(
        'http://127.0.0.1:8000/sessions?' + urllib.parse.urlencode(query),
        headers={'Authorization': 'Bearer ' + os.environ['MINDPROBE_OPERATOR_TOKEN']},
    )
    page = json.load(urllib.request.urlopen(request, timeout=30))
    rows += page['sessions']
    before = page.get('next_cursor')
    if not before:
        break
else:
    print('Activity: WAIT'); print(' - more than 10,000 sessions; the listing was not finished'); sys.exit(1)

blockers, notes = [], []
for row in rows:
    sid, status = row['session_id'], row['status']
    if status in ('starting', 'active', 'paused'):
        blockers.append(f'session {sid} is {status}')
    age = row.get('seconds_since_last_chunk')
    if age is not None and age < 900 and row.get('finalize_state') != 'complete':
        blockers.append(f'session {sid} stored a chunk {age} s ago and its recording is not complete; uploads may still arrive')

db = sqlite3.connect('file:/data/mindprobe.db?mode=ro', uri=True)
now = datetime.now(timezone.utc).replace(tzinfo=None)
def recent(minutes):
    return (now - timedelta(minutes=minutes)).isoformat(sep=' ')
for sid, in db.execute("select id from sessions where status = 'created' and created_at > ?", (recent(180),)):
    notes.append(f'session {sid} was created in the last 3 hours and has not started; check nobody is about to join')
for sid, in db.execute(
        "select s.id from sessions s left join recordings r on r.session_id = s.id "
        "where s.status = 'ended' and s.ended_at > ? and coalesce(r.finalize_state, 'none') != 'complete'", (recent(10),)):
    blockers.append(f'session {sid} ended less than 10 minutes ago and its recording is not complete (upload grace)')
for jid, kind, sid in db.execute("select id, kind, session_id from jobs where status = 'running'"):
    blockers.append(f'job {jid} ({kind}, {sid}) is running')
queued = db.execute("select count(*) from jobs where status = 'queued'").fetchone()[0]
if queued:
    notes.append(f'{queued} queued job(s); the new worker picks them up after the restart')
print(f'Checked {len(rows)} session(s).')
for n in notes:
    print('NOTE:', n)
if blockers:
    print('Activity: WAIT'); [print(' -', b) for b in blockers]; sys.exit(1)
print('Activity: none in progress')
PY
