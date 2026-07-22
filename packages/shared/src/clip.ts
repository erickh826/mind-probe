// Clip manifest schema (spec §12). Mirrors ClipManifestEntry in
// server/app/events.py.

export type CharacterState =
  | "neutral"
  | "thinking"
  | "defensive"
  | "impatient"
  | "refusing";

/** One entry in cases/<case>/clip_manifest.json (spec §12). */
export interface ClipManifestEntry {
  clip_id: string;
  category: string;
  intent: string;
  character_state: CharacterState | string;
  text: string;
  reveals: string[];
  requires: string[];
  forbidden_after: string[];
  duration_ms: number;
  file: string;
  hotkey?: string | null;
}

export interface ClipManifest {
  case_id: string;
  clips: ClipManifestEntry[];
}
