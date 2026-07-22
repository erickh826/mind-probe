"""Case loader (spec §12).

Reads the JSON files under ``cases/<case_id>/`` and exposes them to the API.
The Session Server is the only component that touches the filesystem; the
teacher review app reads case data via REST (審閱後修訂 3).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .events import ClipManifestEntry

CASES_DIR = Path(os.environ.get("MINDPROBE_CASES_DIR", "./cases"))

# The JSON files that make up a case (spec §12).
CASE_FILES = (
    "case_metadata.json",
    "ground_truth.json",
    "suspect_knowledge.json",
    "disclosure_rules.json",
    "wizard_decision_tree.json",
    "clip_manifest.json",
    "rubric.json",
)


def case_dir(case_id: str) -> Path:
    return CASES_DIR / case_id


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def list_cases() -> list[str]:
    """Return available case ids (subdirectories of CASES_DIR)."""
    if not CASES_DIR.is_dir():
        return []
    return sorted(p.name for p in CASES_DIR.iterdir() if p.is_dir())


@lru_cache(maxsize=8)
def load_case(case_id: str) -> dict[str, Any]:
    """Load all JSON files for a case into a single dict keyed by file stem.

    Raises FileNotFoundError if the case directory is missing.
    """
    root = case_dir(case_id)
    if not root.is_dir():
        raise FileNotFoundError(f"case not found: {case_id}")

    data: dict[str, Any] = {"case_id": case_id}
    for filename in CASE_FILES:
        path = root / filename
        if path.exists():
            data[Path(filename).stem] = _read_json(path)
    return data


def load_clip_manifest(case_id: str) -> list[ClipManifestEntry]:
    """Load and validate the clip manifest (spec §12)."""
    path = case_dir(case_id) / "clip_manifest.json"
    raw = _read_json(path)
    clips = raw.get("clips", raw) if isinstance(raw, dict) else raw
    return [ClipManifestEntry.model_validate(c) for c in clips]
