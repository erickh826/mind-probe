"""FastAPI application entrypoint (spec §4, §5).

Wires up CORS (LAN dev), a health check, the WebSocket endpoint, the
recording ingest routes, read-only case REST endpoints used by the teacher
review app (審閱後修訂 3), and the session lifecycle / join-token REST
surface backed by ``models.py``.

It also hosts the teacher **review surface** (spec §3.2 D) -- the recording
stream and its completeness, the unified timeline, the case rubric and the
session audit trail -- authenticated through ``auth.py``. See the section
comment above ``_case_file_path`` for how auth is scoped, and the one above
``_timeline_origin`` for the three clock domains the timeline reconciles.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from starlette.middleware.cors import CORSMiddleware as StarletteCORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from . import __version__, auth, cases, models, recording, websocket, jobs, storage
from .events import EventType
from .models import get_db, init_db
from .recording import router as recording_router

logger = logging.getLogger(__name__)


def _ensure_ws_token_secret() -> bool:
    """Guarantee ``MINDPROBE_WS_TOKEN_SECRET`` is set before any token is signed.

    ``websocket._token_secret()`` reads the secret straight from the
    environment and refuses to sign or verify without one, so a dev machine
    with no secret configured cannot complete a WebSocket upgrade at all.
    Generating a per-process secret here and writing it back to ``os.environ``
    means the join endpoint and the WebSocket verifier see the same key
    without ``websocket.py`` needing to change.

    Production must inject the variable instead (see the implementation notes:
    that belongs in ``docker-compose.yml``, a DevOps/Integration Agent file).

    Returns True if a temporary secret was generated.
    """
    if os.environ.get(websocket.WS_TOKEN_SECRET_ENV):
        return False
    os.environ[websocket.WS_TOKEN_SECRET_ENV] = secrets.token_hex(32)
    # Log that a fallback happened -- never the secret itself (ADR-0004 §3).
    logger.warning(
        "%s is not set; generated a temporary per-process secret for development. "
        "Tokens will not survive a restart and will not validate across replicas. "
        "Set this variable explicitly for any non-dev deployment.",
        websocket.WS_TOKEN_SECRET_ENV,
    )
    return True


def _session_cookie_secure() -> bool:
    """Whether the role-bound session cookie should use the Secure attribute."""
    explicit = os.environ.get("MINDPROBE_COOKIE_SECURE")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    env_name = os.environ.get("MINDPROBE_ENV", "development").strip().lower()
    return env_name not in {"", "dev", "development", "local", "test"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _ensure_ws_token_secret()
    # Tests drive run_once deterministically. Production uses one app worker.
    worker = None
    if os.environ.get("MINDPROBE_JOB_WORKER", "true").lower() not in {"0", "false", "no", "off"}:
        worker = jobs.Worker()
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()


app = FastAPI(title="mind-probe Session Server", version=__version__, lifespan=lifespan)

app.include_router(websocket.router)
app.include_router(recording_router)

# typ=upload is accepted only on the three recording routes below. Every other
# HTTP route, including case JSON and local case media, rejects it. Health
# stays public. upload-login is the code exchange and does not require a bearer.
_UPLOAD_TOKEN_ROUTE = re.compile(
    r"^/recording/(?P<session_id>[^/]+)/(?P<action>chunk|finalize|status)$"
)
_UPLOAD_TOKEN_ACTIONS = frozenset({("POST", "chunk"), ("POST", "finalize"), ("GET", "status")})
_UPLOAD_LOGIN_ROUTE = re.compile(r"^/sessions/[^/]+/upload-login$")


def _reject_upload_token_outside_allowlist(request: Request) -> JSONResponse | None:
    if request.url.path == "/health":
        return None
    if request.method.upper() == "POST" and _UPLOAD_LOGIN_ROUTE.match(request.url.path):
        return None
    token = auth.extract_token(request)
    if not token or auth.is_operator_token(token):
        return None
    try:
        claims = auth.decode_session_token(token)
    except auth.TokenInvalid:
        return None
    if claims.get("typ") != "upload":
        return None
    match = _UPLOAD_TOKEN_ROUTE.match(request.url.path)
    if match is None or (request.method.upper(), match.group("action")) not in _UPLOAD_TOKEN_ACTIONS:
        return JSONResponse(status_code=401, content={"detail": "token_type_mismatch"})
    if claims.get("session_id") != match.group("session_id"):
        return JSONResponse(status_code=401, content={"detail": "session_token_mismatch"})
    return None


@app.middleware("http")
async def upload_token_default_deny(request: Request, call_next):
    rejected = _reject_upload_token_outside_allowlist(request)
    if rejected is not None:
        return rejected
    return await call_next(request)


def _configured_cors_origins() -> list[str]:
    """Origins allowed to call the API. Read per request so tests can set the env."""
    return [
        origin.strip()
        for origin in os.environ.get("MINDPROBE_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]


class EnvCORSMiddleware(StarletteCORSMiddleware):
    """CORS allow-list from ``MINDPROBE_CORS_ORIGINS``, checked on each request."""

    def is_allowed_origin(self, origin: str) -> bool:
        allowed = _configured_cors_origins()
        if "*" in allowed:
            return True
        return origin in allowed


# Registered after the upload-token middleware so this layer is outermost.
# A short-circuit 401 (upload token on any other route, or any earlier auth
# rejection) must still carry Access-Control-Allow-Origin. Credentials stay
# off: auth is Bearer (ADR-0005 §1).
app.add_middleware(
    EnvCORSMiddleware,
    allow_origins=_configured_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health() -> dict:
    """Liveness/readiness check."""
    return {"status": "ok", "service": "mind-probe-server", "version": __version__}


@app.get("/cases")
def get_cases(_actor: auth.Actor = Depends(auth.require_any_actor)) -> dict:
    return {"cases": cases.list_cases()}


@app.get("/cases/{case_id}")
def get_case(
    case_id: str,
    _actor: auth.Actor = Depends(
        auth.require_case_actor("wizard", "teacher", "observer")
    ),
) -> dict:
    # Students are excluded: the bundle holds ground truth and the decision tree.
    try:
        return cases.load_case(case_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")


#: Clip fields a student may see; text/reveals/requires would leak the answers.
_STUDENT_CLIP_FIELDS = ("clip_id", "category", "character_state", "duration_ms", "file")


@app.get("/cases/{case_id}/clips")
def get_case_clips(
    case_id: str,
    actor: auth.Actor = Depends(
        auth.require_case_actor("student", "wizard", "teacher", "observer")
    ),
) -> dict:
    try:
        clips = cases.load_clip_manifest(case_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")
    dumped = [c.model_dump() for c in clips]
    if actor.role == "student":
        dumped = [{k: c[k] for k in _STUDENT_CLIP_FIELDS if k in c} for c in dumped]
    return {"case_id": case_id, "clips": dumped}


# ---------------------------------------------------------------------------
# Session REST surface (spec §4; ADR-0001 §8; ADR-0004 §1–§3)
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, description="e.g. 'S001'.")
    case_id: str = Field(..., min_length=1, description="e.g. 'CASE001'.")


class JoinSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    join_code: str = Field(..., min_length=1, description="One-time session join code.")
    client_id: str | None = Field(
        default=None, description="Opaque per-device id bound into the token."
    )


class UploadLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, description="One-time upload code from the operator.")


class ReviewLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_code: str = Field(..., min_length=1, max_length=200)
    client_id: str | None = Field(default=None, max_length=128)


class ReviewRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(..., min_length=1, max_length=500)


def _session_payload(session: models.Session) -> dict:
    """Serialize a Session row for the REST API."""
    return {
        "session_id": session.id,
        "case_id": session.case_id,
        "status": session.status,
        "snapshot_revision": session.snapshot_revision,
        "pause_reason": session.pause_reason,
        "start_at_ms": session.start_at_ms,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    }


def _event_payload(event: models.Event) -> dict:
    """Serialize an Event row back into the spec §9 envelope field names.

    Keys match ``events.EventEnvelope`` (and therefore
    ``packages/shared/src/events.ts``) exactly, so replaying a session over
    REST needs no new shared type.
    """
    return {
        "session_id": event.session_id,
        "sequence": event.sequence,
        "client_timestamp_ms": event.client_timestamp_ms,
        "server_timestamp_ms": event.server_timestamp_ms,
        "clock_offset_ms": event.clock_offset_ms,
        "source": event.source,
        "event_type": event.event_type,
        "payload": event.payload or {},
    }


def _load_session_or_404(db: DBSession, session_id: str) -> models.Session:
    session = models.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return session


@app.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(
    body: CreateSessionRequest,
    _actor: auth.Actor = Depends(auth.require_operator),
    db: DBSession = Depends(get_db),
) -> dict:
    """Create a session in the ``created`` state (spec §4)."""
    if models.get_session(db, body.session_id) is not None:
        raise HTTPException(
            status_code=409, detail=f"session already exists: {body.session_id}"
        )
    session = models.create_session(db, body.session_id, body.case_id)
    join_codes = models.issue_join_codes(db, body.session_id)
    review_codes = models.issue_review_codes(db, body.session_id)
    payload = _session_payload(session)
    payload["join_codes"] = join_codes
    payload["review_codes"] = review_codes
    return payload


@app.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    _actor: auth.Actor = Depends(
        auth.require_roles(
            "student",
            "wizard",
            "teacher",
            "observer",
            allow_operator=True,
            allowed_typs=("live", "review"),
        )
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    return _session_payload(_load_session_or_404(db, session_id))


@app.get("/sessions/{session_id}/events")
def get_session_events(
    session_id: str,
    _actor: auth.Actor = Depends(
        auth.require_roles(
            "wizard",
            "teacher",
            "observer",
            allow_operator=True,
            ended_exempt=True,
            allowed_typs=("live", "review"),
        )
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """Replay a session's events in sequence order (teacher unified timeline)."""
    _load_session_or_404(db, session_id)
    events = models.load_events(db, session_id)
    return {"session_id": session_id, "events": [_event_payload(e) for e in events]}


@app.post("/sessions/{session_id}/end")
def end_session(
    session_id: str,
    _actor: auth.Actor = Depends(
        auth.require_roles("wizard", "teacher", allow_operator=True)
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """End a session. Idempotent -- ending twice returns the same state."""
    _load_session_or_404(db, session_id)
    try:
        session = models.end_session(db, session_id, commit=False)
        recording.recording_integrity.note_session_ended(db, session_id)
        models.revoke_session_tokens(db, session_id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    # revoke_session_tokens was commit=False, so the listener did not run.
    # Close after the commit is visible to the per-message recheck.
    websocket.manager.schedule_close_live_sockets(
        session_id, code=4401, reason="session_ended"
    )
    return _session_payload(session)


@app.post("/sessions/{session_id}/join")
def join_session(
    session_id: str,
    body: JoinSessionRequest,
    response: Response,
    db: DBSession = Depends(get_db),
) -> dict:
    """Exchange a one-time join code for a role-bound session token (ADR-0005 §1).

    The token is signed by ``websocket.create_session_token()`` and returned in
    the body as ``access_token`` for ``Authorization: Bearer`` and the
    WebSocket ``bearer.<token>`` subprotocol. When ``MINDPROBE_AUTH_COOKIE`` is
    on, the same token is also set as an HttpOnly cookie for same-origin dev.
    Only its SHA-256 digest reaches the audit log (ADR-0004 §3). The role is
    derived from the stored join code; the client never supplies it.
    """
    session = _load_session_or_404(db, session_id)
    if session.status == "ended":
        raise HTTPException(status_code=409, detail="session already ended")
    try:
        join_code = models.consume_join_code(
            db, session_id, body.join_code, commit=False
        )
        token = websocket.create_session_token(
            session_id=session_id,
            role=join_code.role,
            client_id=body.client_id or "temporary-client-id",
        )
        models.record_audit(
            db,
            action=models.AUDIT_TOKEN_ISSUED,
            actor_role=join_code.role,
            session_id=session_id,
            token=token,
            details={
                "client_id": body.client_id or "temporary-client-id",
                "join_code_id": join_code.id,
            },
            commit=False,
        )
        db.commit()
    except models.JoinCodeConsumed:
        db.rollback()
        raise HTTPException(status_code=409, detail="join code already consumed")
    except models.JoinCodeInvalid:
        db.rollback()
        raise HTTPException(status_code=403, detail="invalid join code")
    except websocket.WebSocketAuthError as exc:
        # Only reachable if MINDPROBE_WS_TOKEN_SECRET vanished after startup.
        db.rollback()
        raise HTTPException(status_code=500, detail=exc.reason)
    except Exception:
        db.rollback()
        raise

    if auth.cookie_auth_enabled():
        response.set_cookie(
            key=websocket.WS_SESSION_COOKIE,
            value=token,
            httponly=True,
            secure=_session_cookie_secure(),
            samesite="lax",
            path="/",
            max_age=websocket.WS_TOKEN_TTL_SECONDS,
        )
    # The token is returned in the body as the Bearer credential (ADR-0005 §1).
    return {
        "session_id": session_id,
        "role": join_code.role,
        "status": "ok",
        "access_token": token,
        "expires_at": int(time.time()) + websocket.WS_TOKEN_TTL_SECONDS,
    }


_UPLOAD_REISSUE_OPEN = frozenset({"created", "starting", "active", "paused"})


def _upload_reissue_refusal(db: DBSession, session: models.Session) -> str | None:
    """Why a new upload code must be refused, or None when it may be issued.

    Reissue is allowed before ``/end`` and during the grace window. It is
    refused after grace and after a security revocation. Issuing a code does
    not revoke the original live token.
    """
    if models.upload_security_revoked(db, session.id):
        return "upload_security_revoked"
    if session.status in _UPLOAD_REISSUE_OPEN:
        return None
    if session.status == "ended" and auth.session_upload_grace_open(session):
        return None
    if session.status == "ended":
        return "upload_grace_expired"
    return "upload_reissue_refused"


def _upload_token_exp(session: models.Session, now_s: int) -> int:
    """15 minutes, capped by the grace deadline once the session has ended."""
    exp = now_s + auth.UPLOAD_TOKEN_TTL_SECONDS
    if session.status == "ended":
        if session.ended_at is None:
            return now_s
        grace_end = int(auth._as_utc(session.ended_at).timestamp()) + auth.upload_grace_seconds()
        exp = min(exp, grace_end)
    return exp


def _upload_reissue_facts(session_id: str, db: DBSession) -> dict:
    """Operator-facing snapshot. Does not create a recording row."""
    rec = models.get_recording(db, session_id)
    chunks = recording.stored_chunks(session_id)
    seconds_since_last_chunk = None
    if chunks:
        latest = max(path.stat().st_mtime for _, path in chunks)
        seconds_since_last_chunk = max(0, int(auth.utcnow().timestamp() - latest))
    return {
        "recording_state": rec.recording_state if rec is not None else None,
        "stored_sequences": len(chunks),
        "seconds_since_last_chunk": seconds_since_last_chunk,
    }


def _client_host(request: Request) -> str:
    if request.client is None or not request.client.host:
        return "unknown"
    return request.client.host


@app.post("/sessions/{session_id}/upload-reissue")
def upload_reissue(
    session_id: str,
    _actor: auth.Actor = Depends(auth.require_operator),
    db: DBSession = Depends(get_db),
) -> dict:
    """Issue one upload code. The plaintext is returned once and is not logged.

    Operator token only. A later issue withdraws the previous unused code.
    """
    session = _load_session_or_404(db, session_id)
    refusal = _upload_reissue_refusal(db, session)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    if not auth.upload_issue_limiter.allow(
        session_id,
        limit=auth.UPLOAD_ISSUE_LIMIT,
        window_s=auth.UPLOAD_ISSUE_WINDOW_SECONDS,
        now=auth.upload_rate_now(),
    ):
        raise HTTPException(status_code=429, detail="rate_limited")
    now = auth.utcnow()
    expires_at = now + timedelta(seconds=models.UPLOAD_CODE_TTL_SECONDS)
    try:
        raw_code = models.issue_upload_code(db, session_id, expires_at=expires_at)
        models.record_audit(
            db,
            action=models.AUDIT_UPLOAD_CODE_ISSUED,
            actor_role="operator",
            session_id=session_id,
            token_hash=models.hash_join_code(raw_code),
            details={"code_hash": models.hash_join_code(raw_code)},
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    payload = _upload_reissue_facts(session_id, db)
    payload["upload_code"] = raw_code
    payload["expires_at"] = expires_at.isoformat()
    return payload


@app.post("/sessions/{session_id}/upload-login")
def upload_login(
    session_id: str,
    body: UploadLoginRequest,
    request: Request,
    db: DBSession = Depends(get_db),
) -> dict:
    """Exchange a one-time upload code for a short ``typ=upload`` token.

    The token is not a review grant and is not written to a cookie. Redeeming
    the same code twice yields one success and one 409.
    """
    session = _load_session_or_404(db, session_id)
    if not auth.upload_redeem_limiter.allow(
        _client_host(request),
        limit=auth.UPLOAD_REDEEM_LIMIT,
        window_s=auth.UPLOAD_REDEEM_WINDOW_SECONDS,
        now=auth.upload_rate_now(),
    ):
        raise HTTPException(status_code=429, detail="rate_limited")
    refusal = _upload_reissue_refusal(db, session)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    now_s = int(time.time())
    exp = _upload_token_exp(session, now_s)
    if exp <= now_s:
        raise HTTPException(status_code=409, detail="upload_grace_expired")
    try:
        models.consume_upload_code(db, session_id, body.code, now=auth.utcnow())
        token = websocket.create_session_token(
            session_id=session_id,
            role="student",
            client_id="upload-recovery",
            expires_at_s=exp,
            typ="upload",
            grant_id=session_id,
        )
        models.record_audit(
            db,
            action=models.AUDIT_UPLOAD_TOKEN_ISSUED,
            actor_role="student",
            session_id=session_id,
            token=token,
            details={"typ": "upload", "client_id": "upload-recovery"},
            commit=False,
        )
        db.commit()
    except models.UploadCodeInvalid:
        db.rollback()
        raise HTTPException(status_code=401, detail="upload_code_invalid")
    except models.UploadCodeExpired:
        db.rollback()
        raise HTTPException(status_code=401, detail="upload_code_expired")
    except models.UploadCodeConsumed:
        db.rollback()
        raise HTTPException(status_code=409, detail="upload_code_consumed")
    except websocket.WebSocketAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=exc.reason)
    except Exception:
        db.rollback()
        raise
    return {
        "access_token": token,
        "token_type": "upload",
        "role": "student",
        "expires_at": exp,
    }


def _review_client_id(client_id: str | None) -> str:
    cleaned = (client_id or "").strip()
    return cleaned or "review-client"


def _mint_review_grant(
    db: DBSession,
    *,
    session_id: str,
    role: str,
    client_id: str,
    rotated_from: str | None = None,
) -> dict:
    """Mint one review access/refresh pair. The caller commits and audits.

    The plaintext refresh token is returned to the caller and is not stored.
    """
    now = auth.utcnow()
    access_exp = int(now.timestamp()) + auth.review_access_ttl_seconds()
    refresh_exp = now + timedelta(seconds=auth.review_refresh_ttl_seconds())
    jti = str(uuid.uuid4())
    raw_refresh = secrets.token_urlsafe(32)
    access = websocket.create_session_token(
        session_id=session_id,
        role=role,
        client_id=client_id,
        expires_at_s=access_exp,
        typ="review",
        jti=jti,
    )
    models.insert_review_grant(
        db,
        jti=jti,
        session_id=session_id,
        role=role,
        client_id=client_id,
        refresh_hash=models.hash_token(raw_refresh),
        issued_at=now,
        expires_at=refresh_exp,
        rotated_from=rotated_from,
    )
    return {
        "role": role,
        "access_token": access,
        "refresh_token": raw_refresh,
        "expires_at": access_exp,
        "jti": jti,
    }


def _close_review_sockets(session_id: str, jtis: list[str]) -> None:
    websocket.manager.schedule_close_review_jtis(
        session_id, set(jtis), reason="token_revoked"
    )


def _reject_reused_refresh(
    db: DBSession, session_id: str, grant: models.ReviewGrant, *, actor_role: str
) -> None:
    jtis = models.revoke_review_chain(db, grant.jti)
    models.record_audit(
        db,
        action=models.AUDIT_REVIEW_REVOKE,
        actor_role=actor_role,
        session_id=session_id,
        token_hash=grant.refresh_hash,
        details={"reason": "refresh_reuse", "jti": grant.jti, "revoked_jtis": jtis},
        commit=False,
    )
    db.commit()
    _close_review_sockets(session_id, jtis)


@app.post("/sessions/{session_id}/review-codes")
def regenerate_review_codes(
    session_id: str,
    _actor: auth.Actor = Depends(auth.require_operator),
    db: DBSession = Depends(get_db),
) -> dict:
    """Replace the teacher and observer review codes. Plaintext is returned once."""
    _load_session_or_404(db, session_id)
    codes = models.issue_review_codes(db, session_id)
    return {"session_id": session_id, "review_codes": codes}


@app.post("/sessions/{session_id}/review-login")
def review_login(
    session_id: str,
    body: ReviewLoginRequest,
    request: Request,
    db: DBSession = Depends(get_db),
) -> dict:
    """Exchange a reusable review code for a review access token and refresh token.

    Works while the session is in progress and after it has ended. The code is
    not consumed. Five attempts per client IP per minute.
    """
    if not auth.review_login_limiter.allow(
        _client_host(request),
        limit=auth.REVIEW_LOGIN_LIMIT,
        window_s=auth.REVIEW_LOGIN_WINDOW_SECONDS,
        now=auth.review_rate_now(),
    ):
        raise HTTPException(status_code=429, detail="rate_limited")
    _load_session_or_404(db, session_id)
    client_id = _review_client_id(body.client_id)
    try:
        code_row = models.authenticate_review_code(db, session_id, body.review_code)
    except models.ReviewCodeInvalid:
        models.record_audit(
            db,
            action=models.AUDIT_REVIEW_LOGIN,
            actor_role="unknown",
            session_id=session_id,
            token_hash=models.hash_token(body.review_code),
            details={"result": "invalid_review_code"},
        )
        raise HTTPException(status_code=401, detail="invalid_review_code")
    try:
        minted = _mint_review_grant(
            db,
            session_id=session_id,
            role=code_row.role,
            client_id=client_id,
        )
        models.record_audit(
            db,
            action=models.AUDIT_REVIEW_LOGIN,
            actor_role=code_row.role,
            session_id=session_id,
            token=minted["access_token"],
            details={"result": "ok", "jti": minted["jti"], "client_id": client_id},
            commit=False,
        )
        db.commit()
    except websocket.WebSocketAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=exc.reason) from exc
    except Exception:
        db.rollback()
        raise
    return {
        "role": minted["role"],
        "access_token": minted["access_token"],
        "refresh_token": minted["refresh_token"],
        "expires_at": minted["expires_at"],
    }


@app.post("/sessions/{session_id}/review-refresh")
def review_refresh(
    session_id: str,
    body: ReviewRefreshRequest,
    db: DBSession = Depends(get_db),
) -> dict:
    """Rotate a review refresh token. Reuse of an old refresh revokes the chain."""
    _load_session_or_404(db, session_id)
    presented = models.hash_token(body.refresh_token)
    grant = models.review_grant_by_refresh_hash(db, presented)
    if grant is None or grant.session_id != session_id:
        raise HTTPException(status_code=401, detail="invalid_refresh_token")
    if grant.revoked_at is not None:
        raise HTTPException(status_code=401, detail="revoked_session_token")
    # Reuse wins over expiry: an old refresh that already rotated the chain
    # still revokes the family, even after that old refresh's own deadline.
    if models.review_grant_successor(db, grant.jti) is not None:
        _reject_reused_refresh(db, session_id, grant, actor_role=grant.role)
        raise HTTPException(status_code=401, detail="refresh_reused")
    if models.is_review_grant_revoked(db, grant.jti):
        raise HTTPException(status_code=401, detail="expired_session_token")
    try:
        minted = _mint_review_grant(
            db,
            session_id=session_id,
            role=grant.role,
            client_id=grant.client_id,
            rotated_from=grant.jti,
        )
        models.record_audit(
            db,
            action=models.AUDIT_REVIEW_REFRESH,
            actor_role=grant.role,
            session_id=session_id,
            token=minted["access_token"],
            details={"jti": minted["jti"], "rotated_from": grant.jti},
            commit=False,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = models.review_grant_by_refresh_hash(db, presented)
        if raced is not None and raced.session_id == session_id:
            _reject_reused_refresh(db, session_id, raced, actor_role=raced.role)
        raise HTTPException(status_code=401, detail="refresh_reused") from None
    except websocket.WebSocketAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=exc.reason) from exc
    except Exception:
        db.rollback()
        raise
    return {
        "role": minted["role"],
        "access_token": minted["access_token"],
        "refresh_token": minted["refresh_token"],
        "expires_at": minted["expires_at"],
    }


@app.post("/sessions/{session_id}/review-logout")
def review_logout(
    session_id: str,
    actor: auth.Actor = Depends(
        auth.require_roles("teacher", "observer", typ="review")
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """Revoke the caller's review grant chain. The access token stops working now."""
    claims = auth.decode_session_token(actor.token, session_id=session_id, typ="review")
    jti = str(claims.get("jti") or "")
    jtis = models.revoke_review_chain(db, jti)
    models.record_audit(
        db,
        action=models.AUDIT_REVIEW_LOGOUT,
        actor_role=actor.role,
        session_id=session_id,
        token_hash=actor.token_hash,
        details={"jti": jti, "revoked_jtis": jtis},
        commit=False,
    )
    db.commit()
    _close_review_sockets(session_id, jtis)
    return {"status": "ok"}


@app.post("/sessions/{session_id}/review-revoke")
def review_revoke(
    session_id: str,
    actor: auth.Actor = Depends(auth.require_operator),
    db: DBSession = Depends(get_db),
) -> dict:
    """Operator revoke of every review grant for this session. Codes stay valid."""
    _load_session_or_404(db, session_id)
    jtis = models.revoke_all_review_grants(db, session_id)
    models.record_audit(
        db,
        action=models.AUDIT_REVIEW_REVOKE,
        actor_role="operator",
        session_id=session_id,
        token_hash=actor.token_hash,
        details={"reason": "operator", "revoked_jtis": jtis, "revoked_grants": len(jtis)},
        commit=False,
    )
    db.commit()
    _close_review_sockets(session_id, jtis)
    return {"status": "ok", "revoked_grants": len(jtis)}


# ---------------------------------------------------------------------------
# Teacher review surface (spec §3.2 D, §10.1, §12; ADR-0004 §3, §4)
#
# The review app never touches the filesystem (spec §4), so everything a
# teacher needs after a session arrives through the four endpoints below:
# the student recording and its completeness, the unified timeline, the case
# rubric, and the session's audit trail.
#
# Auth, and where it deliberately is *not* applied:
#
#   - The three ``/sessions/{id}/...`` endpoints require a review-role token
#     (``auth.require_review_actor``): ADR-0004 §3 wants an identified actor
#     behind "誰檢視或下載錄影", which is only meaningful if the reader is
#     authenticated in the first place.
#   - ``/cases/{case_id}/rubric`` is left open, matching its siblings
#     ``/cases/{case_id}`` and ``/cases/{case_id}/clips``. A session-bound
#     token cannot sensibly authorize a case-level resource, and the rubric is
#     static course material rather than session data.
#   - The pre-existing endpoints are untouched. Retrofitting auth onto
#     ``/sessions/{id}/events`` or the recording ingest router is a real
#     hardening task, but it is a behaviour change to another role's surface
#     and belongs in its own slice.
#
# Which endpoints require a ``sessions`` row is decided by ADR-0004 §4's
# retention priority rather than by symmetry: ``/timeline`` needs the row (it
# reads ``case_id`` to enrich clips), while the recording and audit endpoints
# answer from artefacts that are ranked above -- and are designed to outlive --
# that row. They 404 only when the artefact itself is missing.
# ---------------------------------------------------------------------------


def _case_file_path(case_id: str, filename: str) -> Path | None:
    """Resolve one file inside a case directory, or None if it escapes it.

    ``case_id`` arrives from the URL and is used as a path segment, so the
    resolved path is checked back against ``cases.CASES_DIR`` the same way
    ``recording._safe_session_dir`` guards the recording root. Returning None
    for both "outside the root" and "not a file" lets the caller answer 404
    without telling a prober which of the two it was.
    """
    root = cases.CASES_DIR.resolve(strict=False)
    candidate = (root / case_id / filename).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@app.get("/cases/{case_id}/rubric")
def get_case_rubric(
    case_id: str,
    _actor: auth.Actor = Depends(auth.require_case_actor("teacher", "observer")),
) -> dict:
    """Return a case's ``rubric.json`` scoring criteria (spec §12, §13).

    Read straight off disk rather than through ``cases.load_case``, which is
    ``lru_cache``d over the whole case bundle: a teacher editing the rubric
    between sessions should see the change on the next request.
    """
    path = _case_file_path(case_id, "rubric.json")
    if path is None:
        raise HTTPException(
            status_code=404, detail=f"rubric not found for case: {case_id}"
        )
    try:
        rubric = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # The file exists but is unusable -- a content error, not a missing
        # resource, so it must not masquerade as a 404.
        logger.exception("Could not read the rubric for case %s.", case_id)
        raise HTTPException(
            status_code=500, detail=f"rubric for case {case_id} is unreadable"
        ) from exc
    return {"case_id": case_id, "rubric": rubric}


@app.get("/sessions/{session_id}/recording/status")
def get_session_recording_status(
    session_id: str,
    include_chunks: bool = False,
    _actor: auth.Actor = Depends(
        auth.require_roles(
            "teacher",
            "observer",
            allow_operator=True,
            ended_exempt=True,
            allowed_typs=("live", "review"),
        )
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """Chunk inventory and transcode state for a session's recording (spec §10.1).

    Delegates wholesale to ``recording.get_recording_status``, which already
    reconciles what is on disk against the ``recording_chunks`` metadata rows;
    re-deriving completeness here would give the teacher app a second, drifting
    answer to the same question.
    """
    rec = models.get_recording(db, session_id)
    if rec and rec.recording_state in jobs.TOMBSTONES:
        raise HTTPException(status_code=410, detail={"error": "recording_deleted", "recording_state": rec.recording_state})
    try:
        return recording.get_recording_status(
            session_id, db, include_chunks=include_chunks
        )
    except recording.InvalidSessionId as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_session_id", "message": str(exc)},
        ) from exc


@app.get("/sessions/{session_id}/recording")
def get_session_recording(
    session_id: str,
    actor: auth.Actor = Depends(auth.require_roles("teacher", "observer", allow_operator=True,
                                                ended_exempt=True, allowed_typs=("live", "review"))),
    db: DBSession = Depends(get_db),
) -> Response:
    """Issue an audited short-lived MP4 URL, or persist a processing job.

    No FFmpeg or storage network IO runs here. Raw/incomplete takes are never
    exposed as a fallback. Signed URLs are returned, never stored in audit/DB.
    """
    try:
        recording.validate_session_id(session_id)
    except recording.InvalidSessionId as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_session_id", "message": str(exc)},
        ) from exc

    rec = models.get_recording(db, session_id)
    if rec and rec.recording_state in jobs.TOMBSTONES:
        raise HTTPException(status_code=410, detail="recording_deleted")
    if rec is None:
        raise HTTPException(status_code=404, detail=f"recording not found for session: {session_id}")
    if rec.finalize_state != "complete" or rec.needs_operator or rec.incomplete_reason:
        raise HTTPException(status_code=409, detail="recording_not_complete")
    store = storage.get_storage()
    if rec.recording_key:
        # Signing is local work; no HEAD/FFmpeg in this request.
        url = store.presign_get(rec.recording_key)
        auth.record_access_audit(db, action=auth.AUDIT_RECORDING_VIEWED, actor=actor,
                                 session_id=session_id, details={"artefact": "mp4"})
        return JSONResponse({"session_id": session_id, "url": url, "expires_in": storage.PRESIGN_TTL,
                             "recording_state": rec.recording_state})
    kind = "upload" if rec.recording_state == "transcoded" else "transcode"
    latest = db.scalar(select(models.Job).where(
        models.Job.kind == kind, models.Job.session_id == session_id,
    ).order_by(models.Job.id.desc()).limit(1))
    if latest is not None and latest.status == "failed":
        # Polling must not reset the bounded retry budget. OP/teacher-review
        # may explicitly retry through POST /process after fixing the cause.
        raise HTTPException(status_code=409, detail="processing_failed")
    job_id = jobs.enqueue(db, kind, session_id)
    db.commit()
    return JSONResponse({"session_id": session_id, "status": "queued", "job_id": job_id},
                         status_code=202, headers={"Retry-After": "2"})


class DeleteRecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str


@app.delete("/sessions/{session_id}/recording", status_code=202)
def delete_session_recording(
    session_id: str, body: DeleteRecordingRequest,
    actor: auth.Actor = Depends(auth.require_recording_delete_actor),
    db: DBSession = Depends(get_db),
) -> dict:
    if body.confirm != session_id:
        raise HTTPException(status_code=400, detail="confirmation_mismatch")
    try:
        recording.validate_session_id(session_id)
    except recording.InvalidSessionId as exc:
        raise HTTPException(status_code=400, detail="invalid_session_id") from exc
    rec = models.ensure_recording(db, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown session")
    if rec.recording_state not in jobs.TOMBSTONES:
        db.execute(update(models.Recording).where(
            models.Recording.session_id == session_id,
            ~models.Recording.recording_state.in_(jobs.TOMBSTONES),
        ).values(recording_state="deleting", deleted_by=actor.role, updated_at=jobs.now_utc()))
        db.execute(update(models.Job).where(models.Job.session_id == session_id,
            models.Job.status.in_(jobs.ACTIVE), models.Job.kind.in_(("transcode", "upload", "finalize_timeout")),
        ).values(status="cancelled", updated_at=jobs.now_utc()))
        models.record_audit(db, action="recording_delete_requested", actor_role=actor.role,
                            token_hash=actor.token_hash, session_id=session_id,
                            details={}, commit=False)
    jobs.enqueue(db, "delete_recording", session_id)
    db.commit()
    db.refresh(rec)
    return {"session_id": session_id, "recording_state": rec.recording_state}


@app.get("/recording-media/{session_id}")
def get_local_recording_media(session_id: str, expires: int, signature: str,
                             db: DBSession = Depends(get_db)) -> FileResponse:
    try:
        key = storage.recording_key(session_id)
        valid = storage.valid_local_signature(key, expires, signature)
    except (ValueError, recording.InvalidSessionId):
        valid = False
    if not valid:
        raise HTTPException(status_code=401, detail="invalid_media_signature")
    rec = models.get_recording(db, session_id)
    if rec is None or rec.recording_state in jobs.TOMBSTONES:
        raise HTTPException(status_code=410, detail="recording_deleted")
    store = storage.get_storage()
    if not isinstance(store, storage.LocalStorage) or rec.recording_key != key:
        raise HTTPException(status_code=404, detail="recording_not_found")
    path = store.path(key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="recording_not_found")
    return FileResponse(path, media_type="video/mp4", content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# Unified review timeline (spec §3.2 D, §9; ADR-0003)
#
# **Clock domains.** Three different clocks reach this endpoint, and mixing
# them silently is the failure mode worth guarding against:
#
#   1. ``Event.server_timestamp_ms`` -- server *wall* time, stamped by
#      ``websocket._now_ms()`` (~1.7e12).
#   2. ``Session.start_at_ms`` -- server *monotonic* time, set from
#      ``websocket._monotonic_ms() + 2000`` when a start is scheduled
#      (ADR-0003 §5). Process uptime, so a small number.
#   3. ``RecordingChunk.start_ms`` / ``end_ms`` -- the student browser's own
#      offsets from its local session start.
#
# (1) and (2) are therefore *not* subtractable, and t=0 is derived from the
# ``session_started`` event's wall timestamp rather than from ``start_at_ms``
# -- which is still reported, labelled, as advisory metadata. (3) keeps its own
# base and is returned under ``recording_spans``, outside the sorted ``entries``
# list, rather than being interleaved on a scale it does not share.
# ---------------------------------------------------------------------------

#: Playback events that open and close a suspect-clip span (spec §7).
_CLIP_SPAN_START_TYPES = frozenset({EventType.CLIP_STARTED.value})
_CLIP_SPAN_END_TYPES = frozenset(
    {EventType.CLIP_ENDED.value, EventType.CLIP_INTERRUPTED.value}
)

#: Everything the Wizard does, whether it is an annotation (ADR-0002 §7), a
#: lifecycle intervention (ADR-0004 §2) or a playback command (spec §7).
_WIZARD_ACTION_TYPES = frozenset(
    {
        EventType.SESSION_PAUSED.value,
        EventType.SESSION_RESUMED.value,
        EventType.FALLBACK_USED.value,
        EventType.UNCOVERED_QUESTION.value,
        EventType.OPERATOR_ERROR.value,
        EventType.OBSERVER_NOTE.value,
        EventType.CLIP_COMMAND.value,
        EventType.RETURN_TO_IDLE.value,
        EventType.THINKING_STARTED.value,
    }
)

#: Time-sync and heartbeat traffic (ADR-0003). Kept on the timeline because a
#: clock-offset jump is the first thing to look at when audio and video appear
#: misaligned during review.
_SYNC_TYPES = frozenset(
    {EventType.CLOCK_SYNC.value, EventType.PING.value, EventType.PONG.value}
)


def _clip_metadata(case_id: str) -> dict[str, dict]:
    """Clip id -> manifest details, or an empty map if the manifest is unusable.

    Enrichment is a convenience layer over the event log: a case whose manifest
    has gone missing must still produce a timeline, just one without subtitle
    text (spec §12). Mirrors ``cases._safe_clip_manifest``'s tolerance rather
    than raising into the request.
    """
    try:
        clips = cases.load_clip_manifest(case_id)
    except (OSError, ValueError) as exc:
        logger.warning("case %s: clip manifest unavailable (%s)", case_id, exc)
        return {}
    return {
        clip.clip_id: {
            "text": clip.text,
            "intent": clip.intent,
            "category": clip.category,
            "character_state": clip.character_state,
            "manifest_duration_ms": clip.duration_ms,
            "file": clip.file,
        }
        for clip in clips
    }


def _timeline_origin(events: list[models.Event]) -> tuple[int | None, str]:
    """Pick t=0 for the timeline, and say where it came from.

    The ``session_started`` event is the true interview origin. Failing that
    (an aborted session, a replay of a partial log), the earliest stamped event
    is used so the timeline still starts at zero. ``min`` rather than "first by
    sequence": each source numbers its own events, so the lowest sequence is
    not necessarily the earliest arrival.
    """
    stamped = [e for e in events if e.server_timestamp_ms is not None]
    if not stamped:
        return None, "none"
    for event in stamped:
        if event.event_type == EventType.SESSION_STARTED.value:
            return event.server_timestamp_ms, "session_started_event"
    return min(e.server_timestamp_ms for e in stamped), "earliest_event"


def _relative_ms(event: models.Event, origin: int | None) -> int | None:
    """Event time as an offset from t=0, in ms.

    Deliberately *not* clamped to >= 0. Setup events -- ``session_created``,
    ``recording_started`` -- legitimately precede the interview origin, and
    clamping would stack them all at 0 in an arbitrary order, which is worse
    for review than an honest negative offset the UI can floor itself.
    """
    if origin is None or event.server_timestamp_ms is None:
        return None
    return event.server_timestamp_ms - origin


def _base_entry(event: models.Event, origin: int | None) -> dict:
    """The fields every timeline entry carries, whatever its kind."""
    return {
        "t_ms": _relative_ms(event, origin),
        "sequence": event.sequence,
        "source": event.source,
        "event_type": event.event_type,
        "server_timestamp_ms": event.server_timestamp_ms,
        "client_timestamp_ms": event.client_timestamp_ms,
        "clock_offset_ms": event.clock_offset_ms,
        "payload": event.payload or {},
    }


def _build_entries(
    events: list[models.Event], origin: int | None, clips: dict[str, dict]
) -> list[dict]:
    """Fold the event log into the review timeline (spec §3.2 D).

    Clip playback is emitted as *spans* rather than as two loose events: the
    teacher app draws a bar per suspect clip alongside the student recording,
    so ``clip_started`` is paired with the matching ``clip_ended`` /
    ``clip_interrupted`` and the pair becomes one entry carrying both ends and
    the manifest's subtitle text.

    Pairing is keyed on ``clip_id`` and takes the most recent unclosed span for
    that id, so a clip replayed later in the session closes against its own
    start. A close with no open span still produces an entry (flagged
    ``started: False``) -- the event log is the record of what happened, and
    dropping half a pair because the other half never arrived would quietly
    rewrite it.
    """
    entries: list[dict] = []
    open_spans: dict[str, dict] = {}

    for event in events:
        payload = event.payload or {}
        clip_id = payload.get("clip_id")
        event_type = event.event_type

        if event_type in _CLIP_SPAN_START_TYPES:
            entry = _base_entry(event, origin)
            entry.update(
                {
                    "kind": "clip",
                    "clip_id": clip_id,
                    "clip": clips.get(clip_id) if clip_id else None,
                    "started": True,
                    "end_t_ms": None,
                    "end_event_type": None,
                    "duration_ms": None,
                    "played_duration_ms": None,
                }
            )
            entries.append(entry)
            if clip_id:
                open_spans[clip_id] = entry
            continue

        if event_type in _CLIP_SPAN_END_TYPES:
            span = open_spans.pop(clip_id, None) if clip_id else None
            end_t_ms = _relative_ms(event, origin)
            if span is None:
                entry = _base_entry(event, origin)
                entry.update(
                    {
                        "kind": "clip",
                        "clip_id": clip_id,
                        "clip": clips.get(clip_id) if clip_id else None,
                        "started": False,
                        "end_t_ms": end_t_ms,
                        "end_event_type": event_type,
                        "duration_ms": None,
                        "played_duration_ms": payload.get("played_duration_ms"),
                    }
                )
                entries.append(entry)
                continue
            span["end_t_ms"] = end_t_ms
            span["end_event_type"] = event_type
            span["played_duration_ms"] = payload.get("played_duration_ms")
            if span["t_ms"] is not None and end_t_ms is not None:
                span["duration_ms"] = end_t_ms - span["t_ms"]
            if event_type == EventType.CLIP_INTERRUPTED.value:
                # spec §7.2: the reason a clip was cut short is the point of
                # recording the interruption at all.
                span["interrupt_reason"] = payload.get("reason")
            continue

        entry = _base_entry(event, origin)
        if event_type in _WIZARD_ACTION_TYPES:
            entry["kind"] = "wizard_action"
            entry["action"] = event_type
            if clip_id:
                entry["clip_id"] = clip_id
                entry["clip"] = clips.get(clip_id)
            # The four fields the review UI surfaces directly, lifted out of the
            # free-form payload: ADR-0004 §2 pause reason, ADR-0002 §5 fallback
            # category and resolved clip, ADR-0002 §7 uncovered-question note.
            for key in (
                "reason",
                "fallback_category",
                "resolved_clip_id",
                "consecutive_fallbacks",
                "question_text",
                "note",
            ):
                if key in payload:
                    entry[key] = payload[key]
        elif event_type in _SYNC_TYPES:
            entry["kind"] = "sync"
        else:
            entry["kind"] = "lifecycle"
        entries.append(entry)

    # Sort by relative time so the review scrubber reads left to right even
    # when two sources' sequence numbers interleave. Unstamped events (no
    # server_timestamp_ms) sort last rather than crashing the comparison, and
    # `sequence` keeps ties deterministic.
    entries.sort(
        key=lambda e: (e["t_ms"] is None, e["t_ms"] or 0, e["sequence"], e["source"])
    )
    return entries


def _recording_spans(db: DBSession, session_id: str) -> list[dict]:
    """Contiguous runs of uploaded chunks, as capture intervals.

    Returned separately from ``entries`` because ``start_ms``/``end_ms`` are
    the *student browser's* offsets from its own session start, not the server
    wall clock the rest of the timeline uses -- hence the explicit
    ``time_base``. A break between spans is a gap in the capture (a reload, a
    chunk that never landed), which is exactly what a teacher needs to see
    before concluding a student went quiet.
    """
    spans: list[dict] = []
    for chunk in models.get_recording_chunks(db, session_id):
        current = spans[-1] if spans else None
        if current is not None and chunk.sequence == current["last_sequence"] + 1:
            current["last_sequence"] = chunk.sequence
            current["chunk_count"] += 1
            current["size_bytes"] += chunk.size_bytes or 0
            if chunk.end_ms is not None:
                current["end_ms"] = chunk.end_ms
            continue
        spans.append(
            {
                "kind": "recording_span",
                "time_base": "client_relative_to_session_start",
                "first_sequence": chunk.sequence,
                "last_sequence": chunk.sequence,
                "chunk_count": 1,
                "start_ms": chunk.start_ms,
                "end_ms": chunk.end_ms,
                "size_bytes": chunk.size_bytes or 0,
            }
        )
    return spans


def _timeline_summary(entries: list[dict], spans: list[dict]) -> dict:
    """Counts the review UI shows above the scrubber without re-scanning."""
    kinds: dict[str, int] = {}
    actions: dict[str, int] = {}
    for entry in entries:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
        if entry["kind"] == "wizard_action":
            actions[entry["action"]] = actions.get(entry["action"], 0) + 1
    stamped = [e["t_ms"] for e in entries if e["t_ms"] is not None]
    return {
        "entry_count": len(entries),
        "entries_by_kind": kinds,
        "wizard_actions_by_type": actions,
        "clip_count": kinds.get("clip", 0),
        "recording_span_count": len(spans),
        "first_t_ms": min(stamped) if stamped else None,
        "last_t_ms": max(stamped) if stamped else None,
    }


@app.get("/sessions/{session_id}/timeline")
def get_session_timeline(
    session_id: str,
    _actor: auth.Actor = Depends(auth.require_review_actor),
    db: DBSession = Depends(get_db),
) -> dict:
    """The unified review timeline for one session (spec §3.2 D).

    Everything a teacher replays against the student recording, on one time
    base: suspect-clip spans enriched with their manifest subtitle text and
    case category, Wizard interventions (pause reason, fallback category,
    uncovered-question notes), clock-sync events, and the chunk capture
    intervals -- see the clock-domain note above for why the last of those is
    reported alongside ``entries`` rather than inside it.
    """
    session = _load_session_or_404(db, session_id)
    events = models.load_events(db, session_id)
    origin, origin_source = _timeline_origin(events)
    entries = _build_entries(events, origin, _clip_metadata(session.case_id))
    spans = _recording_spans(db, session_id)
    return {
        "session_id": session_id,
        "case_id": session.case_id,
        "status": session.status,
        "snapshot_revision": session.snapshot_revision,
        # t=0. `origin_source` tells the client whether this is the real
        # interview start or a best-effort substitute.
        "t0_server_timestamp_ms": origin,
        "t0_source": origin_source,
        # Advisory only, and on a different clock (server monotonic, ADR-0003
        # §5) -- never subtract it from `t0_server_timestamp_ms`.
        "scheduled_start_at_monotonic_ms": session.start_at_ms,
        "event_count": len(events),
        "entries": entries,
        "recording_spans": spans,
        "summary": _timeline_summary(entries, spans),
    }


@app.get("/sessions/{session_id}/audit")
def get_session_audit(
    session_id: str,
    _actor: auth.Actor = Depends(
        auth.require_roles("teacher", allow_operator=True, typ="review")
    ),
    db: DBSession = Depends(get_db),
) -> dict:
    """The session's audit trail.

    A teacher review grant or the operator may read it. Observer and live
    tokens may not. Review grants are checked strictly, including after end.

    Ordered by ``id`` rather than ``created_at``: insertion order is the true
    sequence, and two rows written inside the same transaction can share a
    timestamp.

    Tokens never leave the server, not even hashed in full -- only an eight
    character prefix of the digest, which is enough to correlate two rows as
    the same actor without handing out a value that could be matched against a
    captured token offline.

    No ``sessions`` row is required: ``AuditLog.session_id`` is deliberately
    not a foreign key so audit history outlives the session it describes.
    """
    rows = db.scalars(
        select(models.AuditLog)
        .where(models.AuditLog.session_id == session_id)
        .order_by(models.AuditLog.id)
    ).all()
    return {
        "session_id": session_id,
        "entries": [
            {
                "id": row.id,
                "action": row.action,
                "actor_role": row.actor_role,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "details": row.details or {},
                "token_hash_prefix": row.token_hash[:8] if row.token_hash else None,
            }
            for row in rows
        ],
    }


@app.api_route("/cases/{case_id}/media/{filename}", methods=["GET", "HEAD"])
def get_local_case_media(case_id: str, filename: str) -> FileResponse:
    """Serve only media in explicitly selected local development mode.

    Production uses R2. Never expose the case directory containing answers,
    decision trees and backups through a static mount.
    """
    if os.environ.get("MINDPROBE_ENV", "").strip().lower() not in {
        "local", "development"
    }:
        raise HTTPException(status_code=404, detail="Not Found")
    if any(char in case_id + filename for char in "/\\:"):
        raise HTTPException(status_code=404, detail="Not Found")
    media_types = {".mp4": "video/mp4", ".png": "image/png"}
    suffix = Path(filename).suffix.lower()
    if suffix not in media_types:
        raise HTTPException(status_code=404, detail="Not Found")
    root = cases.CASES_DIR.resolve()
    media_root = (root / case_id / "media").resolve()
    path = (media_root / filename).resolve()
    if (
        not media_root.is_relative_to(root)
        or not path.is_relative_to(media_root)
        or path.suffix.lower() != suffix
        or not path.is_file()
    ):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(path, media_type=media_types[suffix])

