// UI-level types for the Wizard console (spec §8).
//
// Everything that travels on the wire is imported from `@mind-probe/shared`
// (the locked contract). The types below exist only to describe local console
// state: the three-tier decision tree as loaded from a case, the staged
// selection the Wizard is assembling, and connection health.

import type {
  CharacterState,
  FallbackCategory,
  PlaybackState,
  SessionState,
} from "@mind-probe/shared";

// --- Decision tree (cases/<case>/wizard_decision_tree.json) -----------------

/** Tier-1 hotkeys: the eight thematic categories of spec §8. */
export const CATEGORY_KEYS = [
  "F1",
  "F2",
  "F3",
  "F4",
  "F5",
  "F6",
  "F7",
  "F8",
] as const;

export type CategoryKey = (typeof CATEGORY_KEYS)[number];

/** Tier-2 hotkeys: up to six answer options per category (spec §8). */
export const OPTION_KEYS = ["1", "2", "3", "4", "5", "6"] as const;

export type OptionKey = (typeof OPTION_KEYS)[number];

/** Tier-3 hotkeys: the character tone/state modifiers (spec §8). */
export const TONE_KEYS = ["N", "T", "D", "I", "R"] as const;

export type ToneKey = (typeof TONE_KEYS)[number];

/** Tone key -> the `CharacterState` it selects in the clip manifest. */
export const TONE_TO_CHARACTER_STATE: Record<ToneKey, CharacterState> = {
  N: "neutral",
  T: "thinking",
  D: "defensive",
  I: "impatient",
  R: "refusing",
};

export const TONE_LABELS: Record<ToneKey, string> = {
  N: "中性 Neutral",
  T: "思考 Thinking",
  D: "防衛 Defensive",
  I: "不耐煩 Impatient",
  R: "拒絕 Refusing",
};

/** Fallback labels for a case that does not name its categories. */
export const DEFAULT_CATEGORY_LABELS: Record<CategoryKey, string> = {
  F1: "身份 Identity",
  F2: "人物關係 Relationships",
  F3: "時間線 Timeline",
  F4: "地點 Location",
  F5: "證據 Evidence",
  F6: "矛盾 Contradiction",
  F7: "程序權利 Procedural rights",
  F8: "通用回答 General",
};

/**
 * One tier-2 answer option. `clips` maps a tone key to the clip id that
 * expresses it — `null` means the case author has reserved the slot but no
 * approved clip exists yet, so it must not be dispatched.
 */
export interface DecisionOption {
  label: string;
  clips: Partial<Record<ToneKey, string | null>>;
}

export interface DecisionCategory {
  label: string;
  options: Partial<Record<OptionKey, DecisionOption>>;
}

/** The parsed `wizard_decision_tree.json` tree, keyed by tier-1 hotkey. */
export type DecisionTree = Partial<Record<CategoryKey, DecisionCategory>>;

// --- Staged selection ------------------------------------------------------

/**
 * The selection the Wizard is assembling before pressing `Space`. Spec §8 asks
 * for an answer in two to three keypresses, so the tiers are filled
 * independently and only the resolved clip id gates dispatch.
 */
export interface StagedSelection {
  category: CategoryKey | null;
  option: OptionKey | null;
  tone: ToneKey | null;
}

export const EMPTY_SELECTION: StagedSelection = {
  category: null,
  option: null,
  tone: null,
};

// --- Connection & session state -------------------------------------------

/**
 * Socket health as the console displays it.
 *
 * `degraded` is ADR-0001 §2's missed-heartbeat state: the socket is still open
 * but pongs have stopped arriving, so commands are probably not landing.
 * `closed` is terminal — a 44xx close (bad role, bad token, unknown session,
 * role taken over) that reconnecting cannot fix.
 */
export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "open"
  | "degraded"
  | "reconnecting"
  | "closed";

/** Local mirror of the authoritative `session_snapshot` (ADR-0001 §4). */
export interface SessionView {
  snapshot_revision: number;
  session_state: SessionState;
  playback_state: PlaybackState;
  active_clip: string | null;
  character_state: CharacterState | string;
  cooperation_level: number;
  revealed_facts: string[];
  consecutive_fallbacks: number;
  /** Server monotonic ms of a pending unified start (ADR-0003 §5). */
  start_at_server_ms: number | null;
}

/** State before the first `session_snapshot` lands. */
export const INITIAL_SESSION_VIEW: SessionView = {
  snapshot_revision: 0,
  session_state: "created",
  playback_state: "idle",
  active_clip: null,
  character_state: "neutral",
  cooperation_level: 0,
  revealed_facts: [],
  consecutive_fallbacks: 0,
  start_at_server_ms: null,
};

// --- Console log -----------------------------------------------------------

export type ConsoleLogLevel = "info" | "warn" | "error";

/** One line in the console's rolling activity log. */
export interface ConsoleLogEntry {
  id: string;
  at_ms: number;
  level: ConsoleLogLevel;
  text: string;
}

// --- Fallback --------------------------------------------------------------

export const FALLBACK_CATEGORY_LABELS: Record<FallbackCategory, string> = {
  CLARIFY: "要求澄清 Clarify",
  ONE_AT_A_TIME: "一次一題 One at a time",
  UNKNOWN_OR_UNSURE: "不知道／不確定 Unknown or unsure",
  DECLINE_OR_BOUNDARY: "拒答／界線 Decline or boundary",
};
