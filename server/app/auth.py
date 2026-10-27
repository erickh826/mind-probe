"""REST authentication + access auditing for the teacher review surface.

``verify_token`` is the single claims check for REST and the WebSocket
upgrade. ``websocket._verify_session_token`` extracts the credential (Bearer
subprotocol, or the dev cookie) and calls ``verify_token``; it does not keep
a second copy of the signature, expiry, session or role rules. The
equivalence test ``tests/test_api.py::test_auth_token_rules_match_the_websocket_verifier``
pins that mapping.

**Live tokens and ended sessions.** ``POST /sessions/{id}/end`` revokes every
``token_issued`` digest. Read endpoints marked ended-exempt still accept a
live token whose only revocation reason is ``session_ended``. That exemption
does not apply to ``typ=review``. Review access is a separate grant: every
REST and WebSocket entry checks ``review_grants`` and refuses a missing or
revoked ``jti`` whether or not the session has ended. ``typ=upload`` stays on
the upload grant and is rejected wherever it is not listed in ``allowed_typs``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as DBSession

from . import models, websocket
from .models import get_db

logger = logging.getLogger(__name__)

AUTH_COOKIE_ENV = "MINDPROBE_AUTH_COOKIE"
OPERATOR_TOKEN_ENV = "MINDPROBE_OPERATOR_TOKEN"
OPERATOR_ROLE = "operator"
UPLOAD_GRACE_ENV = "MINDPROBE_UPLOAD_GRACE_SECONDS"
DEFAULT_UPLOAD_GRACE_SECONDS = 600
REVIEW_ACCESS_TTL_ENV = "MINDPROBE_REVIEW_ACCESS_TTL"
REVIEW_REFRESH_TTL_ENV = "MINDPROBE_REVIEW_REFRESH_TTL"
DEFAULT_REVIEW_ACCESS_TTL_SECONDS = 1800
DEFAULT_REVIEW_REFRESH_TTL_SECONDS = 604800
REVIEW_LOGIN_LIMIT = 5
REVIEW_LOGIN_WINDOW_SECONDS = 60
#: Upload recovery tokens are shorter than the two-hour live join token.
UPLOAD_TOKEN_TTL_SECONDS = 15 * 60
UPLOAD_ISSUE_LIMIT = 5
UPLOAD_ISSUE_WINDOW_SECONDS = 10 * 60
UPLOAD_REDEEM_LIMIT = 5
UPLOAD_REDEEM_WINDOW_SECONDS = 60


def utcnow() -> datetime:
    """Clock for grace-window checks. Tests replace this rather than sleeping."""
    return datetime.now(timezone.utc)


def upload_rate_now() -> float:
    """Clock for upload-code rate limits. Tests replace this rather than sleeping."""
    return time.monotonic()


class SlidingWindowLimiter:
    """Process-local counter. ``reset`` keeps tests from sharing a window."""

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, limit: int, window_s: float, now: float) -> bool:
        with self._lock:
            hits = [stamp for stamp in self._events.get(key, []) if now - stamp < window_s]
            if len(hits) >= limit:
                self._events[key] = hits
                return False
            hits.append(now)
            self._events[key] = hits
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


upload_issue_limiter = SlidingWindowLimiter()
upload_redeem_limiter = SlidingWindowLimiter()
review_login_limiter = SlidingWindowLimiter()


def reset_upload_rate_limits() -> None:
    upload_issue_limiter.reset()
    upload_redeem_limiter.reset()


def review_rate_now() -> float:
    """Clock for review-login rate limits. Tests replace this rather than sleeping."""
    return time.monotonic()


def reset_review_rate_limits() -> None:
    review_login_limiter.reset()


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def upload_grace_seconds() -> int:
    """How long a student may keep uploading after ``/end`` (ADR-0005 §7.3)."""
    raw = os.environ.get(UPLOAD_GRACE_ENV, str(DEFAULT_UPLOAD_GRACE_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_UPLOAD_GRACE_SECONDS
    if value < 0:
        return DEFAULT_UPLOAD_GRACE_SECONDS
    return value


def review_access_ttl_seconds() -> int:
    """Lifetime of a ``typ=review`` access token (ADR-0005 §6.5)."""
    return _positive_int_env(REVIEW_ACCESS_TTL_ENV, DEFAULT_REVIEW_ACCESS_TTL_SECONDS)


def review_refresh_ttl_seconds() -> int:
    """Lifetime of a review refresh token, renewed on each rotation."""
    return _positive_int_env(REVIEW_REFRESH_TTL_ENV, DEFAULT_REVIEW_REFRESH_TTL_SECONDS)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def within_upload_grace(
    db: DBSession, session_id: str, reason: str | None, role: str
) -> bool:
    """True when a student token revoked by ``/end`` is still inside the grace window.

    Only ``revoke_reason=session_ended`` qualifies. A security revocation never
    does, and the window does not open for any other role (ADR-0005 §7.3).
    """
    if reason != models.REVOKE_REASON_SESSION_ENDED or role != "student":
        return False
    session = models.get_session(db, session_id)
    if session is None or session.status != "ended" or session.ended_at is None:
        return False
    deadline = _as_utc(session.ended_at) + timedelta(seconds=upload_grace_seconds())
    return utcnow() < deadline


def session_upload_grace_open(session: models.Session) -> bool:
    """True while ``now < ended_at + grace``. Unended sessions are not in grace."""
    if session.status != "ended" or session.ended_at is None:
        return False
    deadline = _as_utc(session.ended_at) + timedelta(seconds=upload_grace_seconds())
    return utcnow() < deadline


def enforce_upload_token_use(db: DBSession, session_id: str) -> None:
    """Refuse a ``typ=upload`` credential whose grant or grace window is closed.

    Checked on every chunk, finalize, and status call. A token minted before
    ``/end`` does not keep working after grace just because its ``exp`` is later.
    ``/end`` does not set the security flag, so grace uploads still pass.
    """
    if models.upload_security_revoked(db, session_id):
        raise HTTPException(status_code=401, detail="revoked_session_token")
    session = models.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="revoked_session_token")
    if session.status == "ended" and not session_upload_grace_open(session):
        raise HTTPException(status_code=401, detail="upload_grace_expired")


def cookie_auth_enabled() -> bool:
    """Cookie tokens are only read for dev / same-origin setups (ADR-0005 §1)."""
    return os.environ.get(AUTH_COOKIE_ENV, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

#: ADR-0004 §3 audit action for "誰檢視或下載錄影". Declared here rather than
#: alongside ``models.AUDIT_*`` because ``models.py`` is outside this task's
#: owned file set; ``AuditLog.action`` is an unconstrained ``String``, so the
#: value is what matters, not where the constant lives. Flagged for relocation
#: in ``tasks/02_implement/implementation_notes.md``.
AUDIT_RECORDING_VIEWED = "recording_viewed"

#: The roles allowed to read the review surface -- "教師/審查員" in
#: ``memo/project_context.md``. ``websocket.VALID_ROLES`` has no "admin";
#: teacher and observer are the two review (and the two multi-connection)
#: roles, so the plan's ``require_teacher_or_admin`` is realized as
#: :func:`require_review_actor`.
REVIEW_ROLES = frozenset({"teacher", "observer"})

#: ``Authorization: Bearer <token>`` is accepted alongside the HttpOnly cookie
#: so a non-browser reviewer (a script, an export job) can authenticate without
#: forging a cookie jar.
BEARER_PREFIX = "bearer "


class TokenInvalid(Exception):
    """A supplied session token failed verification.

    ``reason`` uses the same vocabulary as ``websocket.WebSocketAuthError`` so
    the REST and WebSocket rejection paths stay legible side by side.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Actor:
    """The verified identity behind a REST request.

    ``token_hash`` is the SHA-256 digest ``models.record_audit`` stores; the raw
    token stays in ``token`` only for callers that need to re-hash it, and is
    never serialized into a response (ADR-0004 §3).
    """

    session_id: str
    role: str
    client_id: str
    token: str
    token_hash: str
    #: True when this token has a ``token_revoked`` audit row. Only reachable
    #: on an ended session -- see the module docstring.
    token_revoked: bool = False
    is_operator: bool = False


# ---------------------------------------------------------------------------
# Token verification shared by REST and the WebSocket upgrade (ADR-0005 §6.2)
# ---------------------------------------------------------------------------


def decode_session_token(
    token: str,
    *,
    session_id: str | None = None,
    role: str | None = None,
    roles: frozenset[str] | set[str] | None = None,
    typ: str | None = None,
) -> dict[str, Any]:
    """Verify a session token's signature and claims, returning the claims.

    Checks, in order: canonical encoding, constant-time HMAC comparison,
    ``exp``, ``session_id`` binding, ``role`` binding, and optional ``typ``.
    The session, role and typ checks are skipped when the corresponding
    argument is ``None``. The WebSocket upgrade calls this rather than
    keeping a second copy of the rules.

    The ``session_id`` binding is load-bearing rather than decorative: without
    it, a teacher token minted for session A would read session B's recording.

    Raises :class:`TokenInvalid`.
    """
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        payload_bytes = payload_b64.encode("ascii")
        decoded_payload = websocket._b64decode(payload_b64)
        supplied_signature = websocket._b64decode(signature_b64)
        # Revocation hashes the wire token, so accept only the issuer's exact
        # unpadded base64url encoding (including canonical trailing bits).
        if (
            not payload_b64
            or not signature_b64
            or websocket._b64encode(decoded_payload) != payload_b64
            or websocket._b64encode(supplied_signature) != signature_b64
        ):
            raise ValueError("noncanonical token encoding")
    except (ValueError, UnicodeError) as exc:
        raise TokenInvalid("malformed_session_token") from exc

    try:
        secret = websocket._token_secret()
    except websocket.WebSocketAuthError as exc:
        # Only reachable if MINDPROBE_WS_TOKEN_SECRET vanished after startup.
        raise TokenInvalid(exc.reason) from exc

    expected_signature = hmac.new(
        secret, payload_bytes, hashlib.sha256
    ).digest()

    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise TokenInvalid("invalid_session_token")

    try:
        claims = json.loads(decoded_payload)
    except Exception as exc:
        raise TokenInvalid("malformed_session_token") from exc
    if not isinstance(claims, dict):
        raise TokenInvalid("malformed_session_token")

    try:
        expires_at_s = int(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenInvalid("malformed_session_token") from exc

    if expires_at_s <= int(time.time()):
        raise TokenInvalid("expired_session_token")
    if session_id is not None and claims.get("session_id") != session_id:
        raise TokenInvalid("session_token_mismatch")
    if role is not None and claims.get("role") != role:
        raise TokenInvalid("role_token_mismatch")
    if roles is not None and claims.get("role") not in roles:
        raise TokenInvalid("role_token_mismatch")
    # Tokens minted before review tokens existed carry no ``typ``: they are live.
    if typ is not None and claims.get("typ", "live") != typ:
        raise TokenInvalid("token_type_mismatch")
    return claims


verify_token = decode_session_token


def extract_token(request: Request) -> str | None:
    """Pull the session token off a request: Bearer first, cookie only if enabled."""
    header = request.headers.get("authorization") or ""
    if header[: len(BEARER_PREFIX)].lower() == BEARER_PREFIX:
        candidate = header[len(BEARER_PREFIX) :].strip()
        if candidate:
            return candidate
    if cookie_auth_enabled():
        return request.cookies.get(websocket.WS_SESSION_COOKIE) or None
    return None


def is_operator_token(token: str | None) -> bool:
    expected = os.environ.get(OPERATOR_TOKEN_ENV)
    if not expected or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def _operator_actor(token: str, session_id: str = "") -> "Actor":
    return Actor(
        session_id=session_id,
        role=OPERATOR_ROLE,
        client_id=OPERATOR_ROLE,
        token=token,
        token_hash=models.hash_token(token),
        is_operator=True,
    )


def require_operator(request: Request) -> Actor:
    """Operator-only routes (ADR-0005 §1). 401 when unset, missing or wrong."""
    token = extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing_session_token")
    if not is_operator_token(token):
        raise HTTPException(status_code=401, detail="invalid_operator_token")
    return _operator_actor(token)



# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


_REVIEW_BLOCK_DETAILS = {
    "revoked": "revoked_session_token",
    "expired": "expired_session_token",
    "session": "session_token_mismatch",
    "role": "role_token_mismatch",
}


def enforce_review_grant(db: DBSession, claims: dict[str, Any], session_id: str) -> None:
    """Refuse a review access token whose grant is missing, revoked, or mismatched.

    There is no ended-session exemption. Logout, refresh reuse, and operator
    revoke all show up as ``revoked_at`` on the grant chain.
    """
    jti = claims.get("jti")
    block = models.review_access_block(
        db,
        jti if isinstance(jti, str) else None,
        session_id=session_id,
        role=str(claims.get("role") or ""),
    )
    if block is not None:
        raise HTTPException(status_code=401, detail=_REVIEW_BLOCK_DETAILS[block])


def get_current_actor(
    request: Request,
    session_id: str,
    db: DBSession = Depends(get_db),
) -> Actor:
    """Resolve a live-token actor for a ``/sessions/{session_id}/...`` route.

    Review grants do not pass through here. Routes that accept ``typ=review``
    use :func:`require_roles` so the grant check cannot be skipped.

    ``session_id`` is injected from the path, so the token's session binding is
    always checked against the resource actually being read.

    Revocation (ADR-0004 §3) is enforced *except* on a session whose status is
    ``ended``; see the module docstring for why that exemption exists and what
    it defers. A 401 carries the failure reason so a client can distinguish an
    expired token (re-join) from a mismatched one (wrong session).
    """
    token = extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing_session_token")

    if is_operator_token(token):
        raise HTTPException(
            status_code=403, detail="operator may not use this endpoint"
        )
    try:
        claims = decode_session_token(token, session_id=session_id, typ="live")
    except TokenInvalid as exc:
        raise HTTPException(status_code=401, detail=exc.reason) from exc

    token_hash = models.hash_token(token)
    reason = models.token_revocation_reason(db, session_id, token_hash)
    revoked = reason is not None
    if revoked:
        session = models.get_session(db, session_id)
        if (
            reason != models.REVOKE_REASON_SESSION_ENDED
            or session is None
            or session.status != "ended"
        ):
            raise HTTPException(status_code=401, detail="revoked_session_token")

    return Actor(
        session_id=session_id,
        role=str(claims.get("role") or ""),
        client_id=str(claims.get("sub") or ""),
        token=token,
        token_hash=token_hash,
        token_revoked=revoked,
    )


def require_review_actor(
    request: Request,
    session_id: str,
    db: DBSession = Depends(get_db),
) -> Actor:
    """Teacher or observer via a live token or a review grant.

    Live tokens keep the ended-session read exemption. Review tokens always
    take the strict grant check inside :func:`require_roles`.
    """
    dependency = require_roles(
        "teacher",
        "observer",
        allowed_typs=("live", "review"),
        ended_exempt=True,
    )
    return dependency(request, session_id, db)


def require_recording_delete_actor(
    request: Request,
    session_id: str,
    db: DBSession = Depends(get_db),
) -> Actor:
    """Strict teacher review or OP, Bearer only; no ended-session exemption."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer ") or not header[7:].strip():
        raise HTTPException(status_code=401, detail="missing_session_token")
    return require_roles("teacher", typ="review", allow_operator=True)(request, session_id, db)


def require_roles(
    *roles: str,
    allow_operator: bool = False,
    ended_exempt: bool = False,
    typ: str | None = None,
    allowed_typs: tuple[str, ...] | frozenset[str] | None = None,
    revocation: str | None = None,
) -> Callable[..., Actor]:
    """Build a ``/sessions/{session_id}/...`` dependency for the ADR-0005 §5 table.

    Omitted ``typ`` claims stay ``live``. Callers that do not pass
    ``allowed_typs`` accept only ``live`` (or the single ``typ`` they name).
    The three upload routes pass ``allowed_typs=("live", "upload")``. A
    ``typ=upload`` token is refused everywhere else with ``token_type_mismatch``.

    ``ended_exempt`` (or ``revocation="ended_exempt"``) lets a live token revoked
    by ``/end`` (reason ``session_ended``) keep reading once the session is
    ended. ``revocation="grace_upload"`` is narrower: only a student token, only
    until ``ended_at + MINDPROBE_UPLOAD_GRACE_SECONDS``, and only on the three
    upload endpoints that pass this mode. A token revoked for any other reason
    is always refused. ``typ=upload`` also rechecks the grant flag and, once the
    session has ended, the grace window, even when that token has no revocation
    row and its ``exp`` is still in the future. ``typ=review`` never uses the
    ended exemption: the grant must be present and unrevoked. 403 means a valid
    token with the wrong role; 401 means the token itself is unusable.
    """
    allowed = frozenset(roles)
    if allowed_typs is not None:
        accepted_typs = frozenset(allowed_typs)
        decode_typ = None
    else:
        decode_typ = typ or "live"
        accepted_typs = frozenset({decode_typ})
    if revocation is None:
        mode = "ended_exempt" if ended_exempt else "strict"
    else:
        mode = revocation

    def dependency(
        request: Request,
        session_id: str,
        db: DBSession = Depends(get_db),
    ) -> Actor:
        token = extract_token(request)
        if not token:
            raise HTTPException(status_code=401, detail="missing_session_token")
        if is_operator_token(token):
            if not allow_operator:
                raise HTTPException(
                    status_code=403, detail="operator may not use this endpoint"
                )
            return _operator_actor(token, session_id)
        try:
            claims = decode_session_token(token, session_id=session_id, typ=decode_typ)
        except TokenInvalid as exc:
            raise HTTPException(status_code=401, detail=exc.reason) from exc

        token_typ = str(claims.get("typ", "live"))
        if token_typ not in accepted_typs:
            raise HTTPException(status_code=401, detail="token_type_mismatch")

        role = str(claims.get("role") or "")
        token_hash = models.hash_token(token)
        reason = models.token_revocation_reason(db, session_id, token_hash)
        if token_typ == "review":
            if role not in REVIEW_ROLES:
                raise HTTPException(status_code=401, detail="token_type_mismatch")
            # Review grants are strict. A live-token revocation row, if one
            # were ever written for this digest, is not exempt after /end.
            if reason is not None:
                raise HTTPException(status_code=401, detail="revoked_session_token")
            enforce_review_grant(db, claims, session_id)
        elif reason is not None:
            session = models.get_session(db, session_id)
            if mode == "grace_upload":
                exempt = within_upload_grace(db, session_id, reason, role)
            elif mode == "ended_exempt":
                exempt = (
                    reason == models.REVOKE_REASON_SESSION_ENDED
                    and session is not None
                    and session.status == "ended"
                )
            else:
                exempt = False
            if not exempt:
                raise HTTPException(status_code=401, detail="revoked_session_token")

        if token_typ == "upload":
            if claims.get("grant_id") != session_id:
                raise HTTPException(status_code=401, detail="token_type_mismatch")
            enforce_upload_token_use(db, session_id)

        if role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"role {role!r} may not use this endpoint (allowed: {sorted(allowed)})",
            )
        return Actor(
            session_id=session_id,
            role=role,
            client_id=str(claims.get("sub") or ""),
            token=token,
            token_hash=token_hash,
            token_revoked=reason is not None,
        )

    return dependency


def require_case_actor(
    *roles: str,
    allow_operator: bool = True,
) -> Callable[..., Actor]:
    """Dependency for ``/cases/{case_id}`` routes (no session id in the path).

    The token's own ``session_id`` claim names the session, and that session's
    ``case_id`` must match the path, so a token never unlocks another case.
    """
    allowed = frozenset(roles)

    def dependency(
        request: Request,
        case_id: str,
        db: DBSession = Depends(get_db),
    ) -> Actor:
        actor = _resolve_pathless_actor(request, db, allow_operator=allow_operator)
        if actor.is_operator:
            return actor
        if actor.role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"role {actor.role!r} may not use this endpoint",
            )
        session = models.get_session(db, actor.session_id)
        if session is None or session.case_id != case_id:
            raise HTTPException(status_code=403, detail="case_not_in_session")
        return actor

    return dependency


def require_any_actor(
    request: Request, db: DBSession = Depends(get_db)
) -> Actor:
    """Any valid, unrevoked token, or the operator (``GET /cases``)."""
    return _resolve_pathless_actor(request, db, allow_operator=True)


def _resolve_pathless_actor(
    request: Request, db: DBSession, *, allow_operator: bool
) -> Actor:
    token = extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing_session_token")
    if allow_operator and is_operator_token(token):
        return _operator_actor(token)
    try:
        claims = decode_session_token(token)
    except TokenInvalid as exc:
        raise HTTPException(status_code=401, detail=exc.reason) from exc
    session_id = str(claims.get("session_id") or "")
    role = str(claims.get("role") or "")
    token_typ = str(claims.get("typ", "live"))
    token_hash = models.hash_token(token)
    if token_typ == "upload":
        raise HTTPException(status_code=401, detail="token_type_mismatch")
    if token_typ == "review":
        if role not in REVIEW_ROLES:
            raise HTTPException(status_code=401, detail="token_type_mismatch")
        if models.is_token_hash_revoked(db, session_id, token_hash):
            raise HTTPException(status_code=401, detail="revoked_session_token")
        enforce_review_grant(db, claims, session_id)
    elif token_typ != "live":
        raise HTTPException(status_code=401, detail="token_type_mismatch")
    elif models.is_token_hash_revoked(db, session_id, token_hash):
        raise HTTPException(status_code=401, detail="revoked_session_token")
    return Actor(
        session_id=session_id,
        role=str(claims.get("role") or ""),
        client_id=str(claims.get("sub") or ""),
        token=token,
        token_hash=token_hash,
    )


# ---------------------------------------------------------------------------
# Access auditing (ADR-0004 §3)
# ---------------------------------------------------------------------------


def record_access_audit(
    db: DBSession,
    *,
    action: str,
    actor: Actor,
    session_id: str,
    details: dict[str, Any] | None = None,
) -> models.AuditLog | None:
    """Append an access audit row, never failing the read it describes.

    ``models.record_audit`` is passed ``token_hash`` (already a lowercase
    SHA-256 digest) rather than ``token``, so no raw token reaches the call at
    all -- and ``details`` is assembled from server-side facts only, because
    ``record_audit`` screens that dict for token-shaped strings and *raises*
    instead of writing.

    A failure to audit is logged and swallowed: ADR-0004 §4 ranks the artefact
    above the bookkeeping, and a teacher must not lose access to a recording
    because an audit insert hit a locked database. Returns ``None`` in that
    case.
    """
    safe_details = dict(details or {})
    safe_details.setdefault("client_id", actor.client_id)
    if actor.token_revoked:
        # Post-session access on a revoked token is legitimate but worth
        # distinguishing in the log.
        safe_details.setdefault("post_session_access", True)
    try:
        return models.record_audit(
            db,
            action=action,
            actor_role=actor.role or "unknown",
            session_id=session_id,
            token_hash=actor.token_hash,
            details=safe_details,
        )
    except Exception:  # pragma: no cover - defensive; DB-failure dependent
        db.rollback()
        logger.exception(
            "Could not record the %s audit row for session %s.", action, session_id
        )
        return None
