// REST calls the Wizard console makes against the Session Server.
//
// Only three endpoints are needed: exchange a join code for the role-bound
// cookie (ADR-0001 §8), read which case a session runs, and load that case's
// three-tier decision tree (spec §12, `wizard_decision_tree.json`).
//
// Every request is credentialed: the session token is an HttpOnly cookie, so
// the browser — not this code — attaches it. `credentials: "include"` is a
// no-op on the same-origin dev-proxy path and is what makes the direct
// `VITE_API_BASE_URL` path work at all.

import type { Role } from "@mind-probe/shared";
import { apiUrl } from "./config";
import {
  CATEGORY_KEYS,
  DEFAULT_CATEGORY_LABELS,
  OPTION_KEYS,
  TONE_KEYS,
} from "./types";
import type {
  CategoryKey,
  DecisionCategory,
  DecisionOption,
  DecisionTree,
  OptionKey,
  ToneKey,
} from "./types";

/** A non-2xx response from the Session Server, carrying FastAPI's `detail`. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), { credentials: "include", ...init });
  } catch (cause) {
    throw new ApiError(0, `無法連線至 Session Server：${String(cause)}`);
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      isRecord(body) && typeof body.detail === "string"
        ? body.detail
        : `HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }
  return body;
}

export interface JoinResult {
  session_id: string;
  role: Role | string;
}

/**
 * `POST /sessions/{id}/join` — trades a one-time join code for the HttpOnly
 * `mindprobe_session_token` cookie the WebSocket upgrade then verifies.
 *
 * The role is derived server-side from the join code; this client never asks
 * for one. A code that is not a Wizard code therefore yields a token this
 * console cannot use, which surfaces as a 4401 close on the socket.
 */
export async function joinSession(
  sessionId: string,
  joinCode: string,
  clientId?: string,
): Promise<JoinResult> {
  const body = await request(`/sessions/${encodeURIComponent(sessionId)}/join`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      join_code: joinCode,
      ...(clientId ? { client_id: clientId } : {}),
    }),
  });
  if (!isRecord(body)) {
    throw new ApiError(500, "join 回應格式不正確");
  }
  return {
    session_id: String(body.session_id ?? sessionId),
    role: typeof body.role === "string" ? body.role : "unknown",
  };
}

export interface SessionInfo {
  session_id: string;
  case_id: string;
  status: string;
}

/** `GET /sessions/{id}` — used to discover which case the session runs. */
export async function fetchSession(sessionId: string): Promise<SessionInfo> {
  const body = await request(`/sessions/${encodeURIComponent(sessionId)}`);
  if (!isRecord(body)) {
    throw new ApiError(500, "session 回應格式不正確");
  }
  return {
    session_id: String(body.session_id ?? sessionId),
    case_id: String(body.case_id ?? ""),
    status: String(body.status ?? "unknown"),
  };
}

/**
 * `GET /cases/{case_id}` — the whole case bundle. Only
 * `wizard_decision_tree` is read here; the clip manifest is not needed because
 * the tree's leaves already carry clip ids.
 */
export async function fetchDecisionTree(caseId: string): Promise<DecisionTree> {
  const body = await request(`/cases/${encodeURIComponent(caseId)}`);
  if (!isRecord(body)) {
    return {};
  }
  return parseDecisionTree(body.wizard_decision_tree);
}

/**
 * Normalise `wizard_decision_tree.json` into {@link DecisionTree}.
 *
 * Deliberately lenient: CASE001 defines only F3 and F8 and leaves most leaves
 * `null`, and a half-authored case must render as *disabled* tiers rather than
 * break the console mid-session. Unknown keys are dropped, missing categories
 * simply stay absent.
 */
export function parseDecisionTree(raw: unknown): DecisionTree {
  if (!isRecord(raw) || !isRecord(raw.tree)) {
    return {};
  }
  const tree: DecisionTree = {};
  for (const categoryKey of CATEGORY_KEYS) {
    const node = raw.tree[categoryKey];
    if (!isRecord(node)) {
      continue;
    }
    tree[categoryKey] = parseCategory(categoryKey, node);
  }
  return tree;
}

function parseCategory(
  key: CategoryKey,
  node: Record<string, unknown>,
): DecisionCategory {
  const options: Partial<Record<OptionKey, DecisionOption>> = {};
  const rawOptions = isRecord(node.options) ? node.options : {};
  for (const optionKey of OPTION_KEYS) {
    const option = rawOptions[optionKey];
    if (!isRecord(option)) {
      continue;
    }
    options[optionKey] = {
      label: typeof option.label === "string" ? option.label : `選項 ${optionKey}`,
      clips: parseClips(option.clips),
    };
  }
  return {
    label: typeof node.label === "string" ? node.label : DEFAULT_CATEGORY_LABELS[key],
    options,
  };
}

function parseClips(raw: unknown): Partial<Record<ToneKey, string | null>> {
  const clips: Partial<Record<ToneKey, string | null>> = {};
  if (!isRecord(raw)) {
    return clips;
  }
  for (const toneKey of TONE_KEYS) {
    if (!(toneKey in raw)) {
      continue;
    }
    const value = raw[toneKey];
    clips[toneKey] = typeof value === "string" && value.length > 0 ? value : null;
  }
  return clips;
}

/**
 * Resolve a staged (category, option, tone) triple to a clip id.
 *
 * Returns `null` when the leaf is unauthored — the console must not dispatch a
 * `clip_command` with a missing `clip_id`. When no tone is staged the option's
 * single authored clip is used if it is unambiguous, which is what lets spec
 * §8's two-keypress path work.
 */
export function resolveClipId(
  tree: DecisionTree,
  category: CategoryKey | null,
  option: OptionKey | null,
  tone: ToneKey | null,
): string | null {
  if (category === null || option === null) {
    return null;
  }
  const node = tree[category]?.options?.[option];
  if (!node) {
    return null;
  }
  if (tone !== null) {
    return node.clips[tone] ?? null;
  }
  const authored = TONE_KEYS.map((key) => node.clips[key]).filter(
    (clip): clip is string => typeof clip === "string",
  );
  return authored.length === 1 ? authored[0] : null;
}

/**
 * First authored "thinking" clip in the case tree, for the `T` playback hotkey.
 *
 * Spec §8 asks for a Thinking loop on a dedicated key but the case format has
 * no dedicated slot for one, so it is taken from the decision tree's tone-`T`
 * leaves. Returns `null` for a case that authored none — the console then says
 * so rather than dispatching a `clip_command` with a missing clip id.
 */
export function findThinkingClipId(tree: DecisionTree): string | null {
  for (const categoryKey of CATEGORY_KEYS) {
    const options = tree[categoryKey]?.options;
    if (!options) {
      continue;
    }
    for (const optionKey of OPTION_KEYS) {
      const clip = options[optionKey]?.clips.T;
      if (typeof clip === "string" && clip.length > 0) {
        return clip;
      }
    }
  }
  return null;
}
