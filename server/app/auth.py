"""REST authentication + access auditing for the teacher review surface.

The realtime layer authenticates a WebSocket upgrade with a role-bound HMAC
cookie (ADR-0001 §8). Teacher review happens over REST instead, against the
same token, so this module is the request-side counterpart of
``websocket._verify_session_token()``.

**Why the verification logic is mirrored rather than imported.**
``websocket._verify_session_token(websocket, session_id, role)`` takes a live
``WebSocket`` (it reads the cookie off the socket's headers) and returns
nothing a REST handler can use -- it cannot tell the caller *who* the actor is,
which ADR-0004 §3's "誰檢視或下載錄影" audit row needs. Widening it would mean
editing ``websocket.py``, which belongs to another roadmap role
(``docs/roadmap.md`` §4). So the claims check is re-expressed here, following
the precedent ``models.py`` already sets for ``PAUSE_REASONS`` /
``event_dedup_key``: mirror locally, keep the duplication to the smallest
possible surface (the signing key and the base64 helpers are imported, not
copied), and pin the equivalence with a test --
``tests/test_api.py::test_auth_token_rules_match_the_websocket_verifier``.

**Post-session review and revocation.** ``POST /sessions/{id}/end`` calls
``models.revoke_session_tokens()``, which audit-revokes *every* token issued
for the session, the teacher's included, and ``POST /sessions/{id}/join``
refuses to mint a replacement once ``status == "ended"``. Enforcing revocation
unconditionally would therefore lock the review endpoints at exactly the moment
review begins, with no recovery path. :func:`get_current_actor` resolves this
by exempting ended sessions: a revoked-but-signature-valid, session-bound token
no longer authorizes anything live, but it still *identifies* the actor well
enough for the audit row. See the implementation notes for the follow-up this
defers (a dedicated post-session review token, which needs ``models.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as DBSession

from . import models, websocket
from .models import get_db

logger = logging.getLogger(__name__)

AUTH_COOKIE_ENV = "MINDPROBE_AUTH_COOKIE"
OPERATOR_TOKEN_ENV = "MINDPROBE_OPERATOR_TOKEN"
OPERATOR_ROLE = "operator"


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
# Token verification (mirrors websocket._verify_session_token, ADR-0001 §8)
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

    Performs exactly the four checks ``websocket._verify_session_token`` does,
    in the same order: constant-time HMAC comparison, ``exp``, ``session_id``
    binding, ``role`` binding. The last two are skipped when the corresponding
    argument is ``None``.

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


def get_current_actor(
    request: Request,
    session_id: str,
    db: DBSession = Depends(get_db),
) -> Actor:
    """Resolve the verified actor for a ``/sessions/{session_id}/...`` route.

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
        claims = decode_session_token(token, session_id=session_id)
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


def require_review_actor(actor: Actor = Depends(get_current_actor)) -> Actor:
    """Restrict a route to the review roles (teacher / observer).

    A 403 rather than a 401: the token is valid, the role simply is not one the
    review surface is for.
    """
    if actor.role not in REVIEW_ROLES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"role {actor.role!r} may not read the review surface "
                f"(allowed: {sorted(REVIEW_ROLES)})"
            ),
        )
    return actor


def require_roles(
    *roles: str,
    allow_operator: bool = False,
    ended_exempt: bool = False,
    typ: str | None = None,
) -> Callable[..., Actor]:
    """Build a ``/sessions/{session_id}/...`` dependency for the ADR-0005 §5 table.

    ``ended_exempt`` lets a live token revoked by ``/end`` (reason
    ``session_ended``) keep reading once the session is ended. A token revoked
    for any other reason is always refused. 403 means a valid token with the
    wrong role; 401 means the token itself is unusable.
    """
    allowed = frozenset(roles)

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
            claims = decode_session_token(token, session_id=session_id, typ=typ)
        except TokenInvalid as exc:
            raise HTTPException(status_code=401, detail=exc.reason) from exc

        token_hash = models.hash_token(token)
        reason = models.token_revocation_reason(db, session_id, token_hash)
        if reason is not None:
            session = models.get_session(db, session_id)
            exempt = (
                ended_exempt
                and reason == models.REVOKE_REASON_SESSION_ENDED
                and session is not None
                and session.status == "ended"
            )
            if not exempt:
                raise HTTPException(status_code=401, detail="revoked_session_token")

        role = str(claims.get("role") or "")
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
    token_hash = models.hash_token(token)
    if models.is_token_hash_revoked(db, session_id, token_hash):
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
