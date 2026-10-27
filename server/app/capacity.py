"""Disk headroom for local recordings (design §11 and §14).

``MINDPROBE_DISK_WARN_PCT`` (default 70) and ``MINDPROBE_DISK_MIN_FREE_GB``
(default 5) are read on every check. Usage is the filesystem that holds
``recording.SESSIONS_DIR`` — local chunks and mp4s. The thresholds are in
gibibytes (2^30 bytes). ``/health`` reports the level. ``POST /sessions``
is refused only when free space is under the minimum. A stat failure reports
``unknown`` and does not block session creation.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from . import recording

logger = logging.getLogger(__name__)

DISK_WARN_PCT_ENV = "MINDPROBE_DISK_WARN_PCT"
DISK_MIN_FREE_GB_ENV = "MINDPROBE_DISK_MIN_FREE_GB"
DEFAULT_DISK_WARN_PCT = 70.0
DEFAULT_DISK_MIN_FREE_GB = 5.0
_GIB = 1024**3
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning("%s", message)


def _env_float(name: str, default: float, *, lo: float, hi: float | None) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _warn_once(name, f"Ignoring invalid {name}={raw!r}; using {default}.")
        return default
    if value < lo or (hi is not None and value > hi):
        _warn_once(name, f"Ignoring out-of-range {name}={raw!r}; using {default}.")
        return default
    return value


def probe_path() -> Path:
    candidate = Path(recording.SESSIONS_DIR)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def disk_status() -> dict:
    warn_pct = _env_float(DISK_WARN_PCT_ENV, DEFAULT_DISK_WARN_PCT, lo=0, hi=100)
    min_free_gb = _env_float(DISK_MIN_FREE_GB_ENV, DEFAULT_DISK_MIN_FREE_GB, lo=0, hi=None)
    base = {"warn_pct": warn_pct, "min_free_gb": min_free_gb}
    try:
        usage = shutil.disk_usage(probe_path())
    except OSError as exc:
        _warn_once("disk-usage", f"Disk usage unavailable ({exc}); session creates are not blocked.")
        return {"level": "unknown", "used_pct": None, "free_gb": None, **base}
    free_gb = usage.free / _GIB
    used_pct = (100.0 * usage.used / usage.total) if usage.total else 100.0
    if free_gb < min_free_gb:
        level = "critical"
    elif used_pct > warn_pct:
        level = "warn"
    else:
        level = "ok"
    return {
        "level": level,
        "used_pct": round(used_pct, 1),
        "free_gb": round(free_gb, 2),
        **base,
    }
