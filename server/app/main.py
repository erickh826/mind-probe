"""FastAPI application entrypoint (spec §4, §5).

Wires up CORS (LAN dev), a health check, the WebSocket endpoint, the
recording ingest routes, read-only case REST endpoints used by the teacher
review app (審閱後修訂 3), and the session lifecycle / join-token REST
surface backed by ``models.py``.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from http.cookies import SimpleCookie

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session as DBSession

from . import __version__, cases, models, websocket
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


def _websocket_path_parts(path: str) -> tuple[str, str] | None:
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "ws":
        return None
    return parts[1], parts[2]


def _scope_cookie_value(scope: dict, key: str) -> str | None:
    cookie = SimpleCookie()
    for header_name, header_value in scope.get("headers", []):
        if header_name.lower() == b"cookie":
            cookie.load(header_value.decode("latin1"))
    morsel = cookie.get(key)
    return morsel.value if morsel is not None else None


@contextmanager
def _websocket_guard_db(app_ref: FastAPI) -> Iterator[DBSession]:
    """Open the DB session the WebSocket guard should use.

    Tests override ``models.get_db`` with an in-memory database. Middleware
    runs outside FastAPI dependency injection, so it mirrors the override
    manually when one is present.
    """
    override = app_ref.dependency_overrides.get(models.get_db)
    if override is None:
        with models.session_scope() as db:
            yield db
        return

    dependency = override()
    db = next(dependency)
    try:
        yield db
    finally:
        try:
            next(dependency)
        except StopIteration:
            pass


def _websocket_rejection_reason(
    app_ref: FastAPI, scope: dict, session_id: str
) -> str | None:
    token = _scope_cookie_value(scope, websocket.WS_SESSION_COOKIE)
    if token is None:
        return None
    token_hash = models.hash_token(token)
    with _websocket_guard_db(app_ref) as db:
        session = models.get_session(db, session_id)
        if session is not None and session.status == "ended":
            return "session_ended"
        if models.is_token_hash_revoked(db, session_id, token_hash):
            return "token_revoked"
    return None


class WebSocketSessionGuard:
    """Reject revoked or ended-session WebSocket upgrades before routing."""

    def __init__(self, app, *, app_ref: FastAPI) -> None:
        self.app = app
        self.app_ref = app_ref

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "websocket":
            ws_parts = _websocket_path_parts(scope.get("path", ""))
            if ws_parts is not None:
                session_id, _role = ws_parts
                try:
                    reason = _websocket_rejection_reason(
                        self.app_ref, scope, session_id
                    )
                except OperationalError:
                    logger.warning(
                        "Skipping WebSocket session revocation check because "
                        "the persistence schema is not initialized."
                    )
                    reason = None
                except SQLAlchemyError:
                    logger.exception("Failed to check WebSocket session revocation.")
                    reason = "session_state_unavailable"
                if reason is not None:
                    await send(
                        {
                            "type": "websocket.close",
                            "code": 4401,
                            "reason": reason,
                        }
                    )
                    return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _ensure_ws_token_secret()
    yield


app = FastAPI(title="mind-probe Session Server", version=__version__, lifespan=lifespan)

# MVP runs on one LAN; allow all origins in dev. Tighten for pilot.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(WebSocketSessionGuard, app_ref=app)

app.include_router(websocket.router)
app.include_router(recording_router)


@app.get("/health")
def health() -> dict:
    """Liveness/readiness check."""
    return {"status": "ok", "service": "mind-probe-server", "version": __version__}


@app.get("/cases")
def get_cases() -> dict:
    return {"cases": cases.list_cases()}


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    try:
        return cases.load_case(case_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")


@app.get("/cases/{case_id}/clips")
def get_case_clips(case_id: str) -> dict:
    try:
        clips = cases.load_clip_manifest(case_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"case not found: {case_id}")
    return {"case_id": case_id, "clips": [c.model_dump() for c in clips]}


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
def create_session(body: CreateSessionRequest, db: DBSession = Depends(get_db)) -> dict:
    """Create a session in the ``created`` state (spec §4)."""
    if models.get_session(db, body.session_id) is not None:
        raise HTTPException(
            status_code=409, detail=f"session already exists: {body.session_id}"
        )
    session = models.create_session(db, body.session_id, body.case_id)
    join_codes = models.issue_join_codes(db, body.session_id)
    payload = _session_payload(session)
    payload["join_codes"] = join_codes
    return payload


@app.get("/sessions/{session_id}")
def get_session(session_id: str, db: DBSession = Depends(get_db)) -> dict:
    return _session_payload(_load_session_or_404(db, session_id))


@app.get("/sessions/{session_id}/events")
def get_session_events(session_id: str, db: DBSession = Depends(get_db)) -> dict:
    """Replay a session's events in sequence order (teacher unified timeline)."""
    _load_session_or_404(db, session_id)
    events = models.load_events(db, session_id)
    return {"session_id": session_id, "events": [_event_payload(e) for e in events]}


@app.post("/sessions/{session_id}/end")
def end_session(session_id: str, db: DBSession = Depends(get_db)) -> dict:
    """End a session. Idempotent -- ending twice returns the same state."""
    _load_session_or_404(db, session_id)
    try:
        session = models.end_session(db, session_id, commit=False)
        models.revoke_session_tokens(db, session_id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _session_payload(session)


@app.post("/sessions/{session_id}/join")
def join_session(
    session_id: str,
    body: JoinSessionRequest,
    response: Response,
    db: DBSession = Depends(get_db),
) -> dict:
    """Exchange a one-time join code for a role-bound session token (ADR-0001 §8).

    The token is signed by ``websocket.create_session_token()`` -- called
    directly rather than reimplemented -- so the cookie this sets is exactly
    what ``websocket._verify_session_token()`` expects at upgrade time. It is
    returned only as an HttpOnly cookie, never in the response body, and only
    its SHA-256 digest reaches the audit log (ADR-0004 §3). The role is
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

    response.set_cookie(
        key=websocket.WS_SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=_session_cookie_secure(),
        samesite="lax",
        path="/",
        max_age=websocket.WS_TOKEN_TTL_SECONDS,
    )
    return {"session_id": session_id, "role": join_code.role, "status": "ok"}
