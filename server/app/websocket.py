"""WebSocket endpoint + connection manager (spec §4, §9).

Wizard and student clients hold a persistent WebSocket to the server for
real-time commands (clip play/stop, session control) and event streaming.
This is a skeleton: routing/persistence are stubbed for the init scaffold.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .events import EventEnvelope, EventType, Source

router = APIRouter()


class ConnectionManager:
    """Tracks live WebSocket connections per session and role.

    Reconnect + missed-sequence backfill (審閱後修訂 5) is a TODO.
    """

    def __init__(self) -> None:
        # session_id -> {role -> WebSocket}
        self._connections: dict[str, dict[str, WebSocket]] = defaultdict(dict)

    async def connect(self, session_id: str, role: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[session_id][role] = ws

    def disconnect(self, session_id: str, role: str) -> None:
        self._connections.get(session_id, {}).pop(role, None)

    async def send_to(self, session_id: str, role: str, message: dict) -> None:
        ws = self._connections.get(session_id, {}).get(role)
        if ws is not None:
            await ws.send_json(message)

    async def broadcast(
        self, session_id: str, message: dict, *, exclude: str | None = None
    ) -> None:
        for role, ws in self._connections.get(session_id, {}).items():
            if role != exclude:
                await ws.send_json(message)


manager = ConnectionManager()


def _now_ms() -> int:
    return int(time.time() * 1000)


@router.websocket("/ws/{session_id}/{role}")
async def session_ws(websocket: WebSocket, session_id: str, role: str) -> None:
    """One socket per (session, role). role is 'student' or 'wizard'.

    TODO: validate session-level token (審閱後修訂 6) before accepting.
    """
    await manager.connect(session_id, role, websocket)
    try:
        while True:
            raw = await websocket.receive_json()
            # Stamp server receive time and validate against the envelope schema.
            raw.setdefault("session_id", session_id)
            raw.setdefault("source", role)
            envelope = EventEnvelope.model_validate(raw)
            envelope.server_timestamp_ms = _now_ms()

            # Cheap ping/pong for clock-offset estimation (spec §9).
            if envelope.event_type == EventType.PING:
                await manager.send_to(
                    session_id,
                    role,
                    {
                        "session_id": session_id,
                        "sequence": envelope.sequence,
                        "client_timestamp_ms": envelope.client_timestamp_ms,
                        "server_timestamp_ms": envelope.server_timestamp_ms,
                        "source": Source.SERVER.value,
                        "event_type": EventType.PONG.value,
                        "payload": {},
                    },
                )
                continue

            # TODO: persist envelope via models.Event, then route:
            #   wizard clip_command -> forward to student
            #   student events      -> forward to wizard/teacher observers
            await manager.broadcast(
                session_id, envelope.model_dump(mode="json"), exclude=role
            )
    except WebSocketDisconnect:
        manager.disconnect(session_id, role)
