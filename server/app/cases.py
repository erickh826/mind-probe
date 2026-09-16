"""Case loader (spec §12).

Reads the JSON files under ``cases/<case_id>/`` and exposes them to the API.
The Session Server is the only component that touches the filesystem; the
teacher review app reads case data via REST (審閱後修訂 3).

It also owns the *fallback resolution* half of ADR-0002's hybrid model: the
Wizard picks a semantic category, and the server picks the actual approved clip
for that category out of this case's manifest (ADR-0002 §1, §2). Everything
below the manifest loader implements that resolution and is deliberately pure
-- ``websocket.py`` supplies the live session state, this module never reads it.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .events import ClipManifestEntry

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Fallback resolution (ADR-0002 §1–§4)
# ---------------------------------------------------------------------------

#: The four fixed semantic categories a Wizard may request (ADR-0002 §2). The
#: Wizard presses a *category*, never a clip id -- which clip expresses that
#: category is a server decision, so two students asking the same unanswerable
#: question get a consistent, auditable response.
FALLBACK_CATEGORIES: tuple[str, ...] = (
    "CLARIFY",
    "ONE_AT_A_TIME",
    "UNKNOWN_OR_UNSURE",
    "DECLINE_OR_BOUNDARY",
)

#: The Wizard-supplied reasons listed in ADR-0002 §7. This is the picklist the
#: Wizard UI should offer, *not* a closed enum: the ADR calls them "可選"
#: (available) reasons, and refusing a live fallback over a taxonomy string
#: would be the wrong failure mode. ``websocket.py`` records whatever arrives.
FALLBACK_REASONS: tuple[str, ...] = (
    "no_matching_response",
    "ambiguous_question",
    "compound_question",
    "outside_character_knowledge",
    "disclosure_not_allowed",
    "wizard_could_not_find_clip",
    "technical_delay",
)

#: ``ClipManifestEntry.category`` value marking a clip as fallback material
#: rather than a case answer. ADR-0002 §3: a semantically fitting *case* clip is
#: a normal response, not a fallback -- keeping the two apart is what makes the
#: coverage metric meaningful.
FALLBACK_CLIP_CATEGORY = "fallback"

#: Substrings identifying which category a fallback clip belongs to. Matched
#: against ``intent`` + ``clip_id`` because manifests spell the same category
#: several ways (CASE001 uses intent ``ask_clarify`` and clip id
#: ``C001_FALLBACK_CLARIFY_01`` for ``CLARIFY``).
_FALLBACK_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "CLARIFY": ("clarify",),
    "ONE_AT_A_TIME": ("one_at_a_time", "one_question_at_a_time"),
    "UNKNOWN_OR_UNSURE": ("unknown", "unsure", "memory_gap"),
    "DECLINE_OR_BOUNDARY": ("decline", "boundary", "refuse"),
}

#: The four generic fallback clips of spec §6, used when a case ships no
#: approved clip of its own for a category. These ids are a contract with the
#: shared media library; the clips themselves are Case/Content Agent territory.
GENERIC_FALLBACK_CLIPS: dict[str, str] = {
    "CLARIFY": "GEN_CLARIFY_01",
    "ONE_AT_A_TIME": "GEN_ONE_AT_A_TIME_01",
    "UNKNOWN_OR_UNSURE": "GEN_UNKNOWN_01",
    "DECLINE_OR_BOUNDARY": "GEN_DECLINE_01",
}


def _safe_clip_manifest(case_id: str) -> list[ClipManifestEntry]:
    """Clip manifest for `case_id`, or an empty list if it cannot be read.

    Resolution runs on the realtime path: a missing or malformed manifest must
    degrade to the generic clips of :data:`GENERIC_FALLBACK_CLIPS` rather than
    raise into the WebSocket handler. ``FileNotFoundError`` is an ``OSError``;
    both ``json.JSONDecodeError`` and pydantic's ``ValidationError`` are
    ``ValueError``s.
    """
    try:
        return load_clip_manifest(case_id)
    except (OSError, ValueError) as exc:
        logger.warning("case %s: clip manifest unavailable (%s)", case_id, exc)
        return []


def _matches_category(clip: ClipManifestEntry, category: str) -> bool:
    haystack = f"{clip.intent} {clip.clip_id}".lower()
    return any(kw in haystack for kw in _FALLBACK_CATEGORY_KEYWORDS[category])


def fallback_clips(case_id: str, category: str) -> list[ClipManifestEntry]:
    """Approved fallback clips for one semantic category, in manifest order.

    Manifest order is the tie-breaker throughout resolution, so the same state
    always yields the same clip (ADR-0002 §1: no random rotation).
    """
    if category not in _FALLBACK_CATEGORY_KEYWORDS:
        raise ValueError(f"unknown fallback category: {category}")
    return [
        clip
        for clip in _safe_clip_manifest(case_id)
        if clip.category.lower() == FALLBACK_CLIP_CATEGORY
        and _matches_category(clip, category)
    ]


def is_fallback_clip(case_id: str, clip_id: str) -> bool:
    """True when `clip_id` is fallback material rather than a case answer.

    ``websocket.py`` uses this to decide whether a clip that starts playing
    should reset the consecutive-fallback counter (ADR-0002 §5).
    """
    if clip_id in GENERIC_FALLBACK_CLIPS.values():
        return True
    return any(
        clip.clip_id == clip_id and clip.category.lower() == FALLBACK_CLIP_CATEGORY
        for clip in _safe_clip_manifest(case_id)
    )


def resolve_fallback_clip(
    case_id: str,
    category: str,
    character_state: str = "neutral",
    revealed_facts: list[str] | None = None,
    last_fallback_clip_id: str | None = None,
) -> str:
    """Resolve a Wizard-selected category to one approved clip id (ADR-0002 §1).

    The category is never changed: an unavailable ``CLARIFY`` resolves to the
    generic clarify clip, never to a "don't remember" or a refusal, because
    those express different facts and attitudes (ADR-0002 §4). Within the
    category, selection is deterministic:

    1. take this case's approved clips for the category, in manifest order;
    2. drop any whose ``forbidden_after`` intersects `revealed_facts`, so a
       fallback cannot contradict something already disclosed (spec §12);
    3. prefer clips matching `character_state`; if none match, keep the rest
       rather than switching category;
    4. skip `last_fallback_clip_id` when another candidate exists, so the same
       clip is not played twice in a row (ADR-0002 §4);
    5. fall back to the generic clip for the category when the case has none.

    Raises ValueError for a category outside :data:`FALLBACK_CATEGORIES`.
    """
    revealed = set(revealed_facts or ())
    candidates = [
        clip
        for clip in fallback_clips(case_id, category)
        if not revealed.intersection(clip.forbidden_after)
    ]
    if not candidates:
        return GENERIC_FALLBACK_CLIPS[category]

    in_state = [c for c in candidates if c.character_state == character_state]
    preferred = in_state or candidates

    for clip in preferred:
        if clip.clip_id != last_fallback_clip_id:
            return clip.clip_id
    # Only one approved clip for this state: repeating it beats silently
    # changing what the character says.
    return preferred[0].clip_id
