"""SQLAlchemy models + SQLite engine (spec §5, §9, §10).

The Session Server owns all local storage. Sessions and their events are
persisted to SQLite; recording chunks/media live on the local filesystem
under ``sessions/`` and ``cases/`` (see ``recording.py`` / ``cases.py``).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

DB_URL = os.environ.get("MINDPROBE_DB_URL", "sqlite:///./sessions/mind-probe.sqlite")

# check_same_thread=False so the async FastAPI workers can share the engine.
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Session(Base):
    """A single interview session (spec §3, §9)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. "S001"
    case_id: Mapped[str] = mapped_column(String, index=True)
    # session-level access token (審閱後修訂 6)
    token: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="created")
    start_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list["Event"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Event(Base):
    """A persisted event envelope (spec §9)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, index=True)
    client_timestamp_ms: Mapped[int] = mapped_column(Integer)
    server_timestamp_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clock_offset_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    session: Mapped[Session] = relationship(back_populates="events")


class RecordingChunk(Base):
    """Metadata for an uploaded MediaRecorder chunk (spec §10.1)."""

    __tablename__ = "recording_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String, nullable=True)
    upload_status: Mapped[str] = mapped_column(String, default="received")
    path: Mapped[str | None] = mapped_column(Text, nullable=True)


def init_db() -> None:
    """Create tables. Called on server startup (spec §5)."""
    os.makedirs("./sessions", exist_ok=True)
    Base.metadata.create_all(bind=engine)
