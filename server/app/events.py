"""Event envelope schema (spec §9 標準事件封套).

All events exchanged between the student/wizard clients and the Session Server
use a single envelope. Event-type-specific fields (e.g. ``clip_id``,
``played_duration_ms``, ``reason``) live inside the ``payload`` dict
(審閱後修訂 2). This mirrors ``packages/shared/src/events.ts``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Source(str, Enum):
    """Origin of an event."""

    STUDENT = "student"
    WIZARD = "wizard"
    SERVER = "server"


class EventType(str, Enum):
    """Known event types. Extend as the protocol grows (spec §7, §9)."""

    # Session lifecycle
    SESSION_CREATED = "session_created"
    SESSION_STARTED = "session_started"
    SESSION_PAUSED = "session_paused"
    SESSION_RESUMED = "session_resumed"
    SESSION_ENDED = "session_ended"

    # Clock sync (spec §9)
    PING = "ping"
    PONG = "pong"
    CLOCK_SYNC = "clock_sync"

    # Clip playback (spec §7)
    CLIP_COMMAND = "clip_command"  # wizard -> server -> student: play a clip
    CLIP_STARTED = "clip_started"
    CLIP_ENDED = "clip_ended"
    CLIP_INTERRUPTED = "clip_interrupted"  # student barge-in (spec §7.2)
    RETURN_TO_IDLE = "return_to_idle"
    THINKING_STARTED = "thinking_started"

    # Wizard annotations
    FALLBACK_USED = "fallback_used"
    UNCOVERED_QUESTION = "uncovered_question"  # M: mark uncovered question
    OPERATOR_ERROR = "operator_error"
    OBSERVER_NOTE = "observer_note"

    # Recording (spec §10)
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    CHUNK_UPLOADED = "chunk_uploaded"


class EventEnvelope(BaseModel):
    """Standard event envelope (spec §9).

    Example (clip_interrupted, spec §7.2)::

        {
          "session_id": "S001",
          "sequence": 42,
          "client_timestamp_ms": 68420,
          "server_timestamp_ms": 68455,
          "clock_offset_ms": 35,
          "source": "wizard",
          "event_type": "clip_interrupted",
          "payload": {"clip_id": "C001_TIMELINE_03",
                      "played_duration_ms": 2750,
                      "reason": "student_barge_in"}
        }
    """

    session_id: str = Field(..., description="Server-assigned session id, e.g. 'S001'.")
    sequence: int = Field(..., ge=0, description="Monotonic per-session sequence number.")
    client_timestamp_ms: int = Field(
        ..., description="Client-relative time since session start_at, in ms."
    )
    server_timestamp_ms: int | None = Field(
        default=None, description="Server receive time in ms; filled in by the server."
    )
    clock_offset_ms: int | None = Field(
        default=None, description="Estimated client->server clock offset (spec §9)."
    )
    source: Source
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class ClipManifestEntry(BaseModel):
    """A single clip entry in ``cases/<case>/clip_manifest.json`` (spec §12).

    Mirrors ``packages/shared/src/clip.ts``.
    """

    clip_id: str
    category: str
    intent: str
    character_state: str
    text: str
    reveals: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    forbidden_after: list[str] = Field(default_factory=list)
    duration_ms: int
    file: str
    hotkey: str | None = None
