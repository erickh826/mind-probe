"""Event envelope schema validation tests (spec §9)."""

import pytest
from pydantic import ValidationError

from app.events import ClipManifestEntry, EventEnvelope, EventType, Source


def test_clip_interrupted_envelope_matches_spec():
    """The §7.2 example must validate exactly as written."""
    event = EventEnvelope.model_validate(
        {
            "session_id": "S001",
            "sequence": 42,
            "client_timestamp_ms": 68420,
            "server_timestamp_ms": 68455,
            "clock_offset_ms": 35,
            "source": "wizard",
            "event_type": "clip_interrupted",
            "payload": {
                "clip_id": "C001_TIMELINE_03",
                "played_duration_ms": 2750,
                "reason": "student_barge_in",
            },
        }
    )
    assert event.source is Source.WIZARD
    assert event.event_type is EventType.CLIP_INTERRUPTED
    assert event.payload["clip_id"] == "C001_TIMELINE_03"


def test_payload_defaults_to_empty_dict():
    event = EventEnvelope(
        session_id="S001",
        sequence=1,
        client_timestamp_ms=100,
        source=Source.SERVER,
        event_type=EventType.SESSION_CREATED,
    )
    assert event.payload == {}
    assert event.server_timestamp_ms is None


def test_negative_sequence_rejected():
    with pytest.raises(ValidationError):
        EventEnvelope(
            session_id="S001",
            sequence=-1,
            client_timestamp_ms=0,
            source=Source.WIZARD,
            event_type=EventType.PING,
        )


def test_unknown_event_type_rejected():
    with pytest.raises(ValidationError):
        EventEnvelope(
            session_id="S001",
            sequence=0,
            client_timestamp_ms=0,
            source=Source.WIZARD,
            event_type="not_a_real_event",
        )


def test_clip_manifest_entry_matches_spec():
    """The §12 clip manifest example must validate."""
    clip = ClipManifestEntry.model_validate(
        {
            "clip_id": "C001_TIMELINE_LEAVE_02",
            "category": "timeline",
            "intent": "ask_leave_time",
            "character_state": "defensive",
            "text": "我已經說了，我大約九時左右離開。",
            "reveals": ["claimed_leave_time"],
            "requires": [],
            "forbidden_after": ["actual_leave_time_revealed"],
            "duration_ms": 4380,
            "file": "media/C001_TIMELINE_LEAVE_02.mp4",
            "hotkey": "F3-2-D",
        }
    )
    assert clip.clip_id == "C001_TIMELINE_LEAVE_02"
    assert clip.reveals == ["claimed_leave_time"]
