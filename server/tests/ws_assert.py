"""Assert how a WebSocket upgrade is refused.

A refusal must be accepted first and then closed. Closing before ``accept()``
makes uvicorn answer HTTP 403, which a browser reports only as close 1006 --
no code, no reason -- so the client cannot tell a revoked token from a busy
role. TestClient shows the code either way, so checking the code alone does
not catch that: entering the connection is what proves the accept.
"""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from app.websocket import WS_PROTOCOL


def assert_upgrade_refused(
    connect,
    *,
    code: int,
    reason: str | None = None,
    subprotocol: str | None = WS_PROTOCOL,
) -> None:
    """``connect`` (a ``websocket_connect(...)`` context) is accepted, then closed.

    No frame may arrive before the close, and only ``subprotocol`` may be
    selected -- never a ``bearer.*`` credential.
    """
    try:
        sock = connect.__enter__()
    except WebSocketDisconnect as exc:
        pytest.fail(
            f"upgrade closed before accept ({exc.code} {exc.reason}); "
            "a browser would only see 1006"
        )
    try:
        assert sock.accepted_subprotocol == subprotocol
        with pytest.raises(WebSocketDisconnect) as exc_info:
            sock.receive_text()
            pytest.fail("a frame arrived before the refusal closed the socket")
    finally:
        connect.__exit__(None, None, None)
    assert exc_info.value.code == code
    if reason is not None:
        assert exc_info.value.reason == reason
