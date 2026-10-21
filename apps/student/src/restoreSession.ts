// Restore a student console after reload.
//
// Ordinary session and clip reads are refused once `/end` revokes the live
// token. Chunk upload, finalize, and `GET /recording/{id}/status` stay allowed
// for the grace window. Restoration must use that status plus the saved
// credential and IndexedDB, and must not treat a 401 from the session or clip
// routes as "forget this login".

import type { ClipManifestEntry } from "@mind-probe/shared";
import type { StoredJoinedSession } from "@mind-probe/shared";
import { ApiError } from "./apiError.ts";

export type RestoredStudent =
  | {
      kind: "live";
      sessionId: string;
      caseId: string;
      clips: ClipManifestEntry[];
      accessToken: string;
    }
  | {
      kind: "upload-only";
      sessionId: string;
      accessToken: string;
    }
  | { kind: "rejected"; message: string }
  | { kind: "unavailable" };

export interface RestoreProbes {
  fetchSession: (sessionId: string) => Promise<{ session_id: string; case_id: string }>;
  fetchClipManifest: (caseId: string) => Promise<ClipManifestEntry[]>;
  fetchUploadStatus: (sessionId: string) => Promise<unknown>;
  clearJoinedSession: (sessionId: string) => void;
}

function refused(cause: unknown): boolean {
  return cause instanceof ApiError && (cause.status === 401 || cause.status === 403 || cause.status === 404);
}

/** Shown with a Retry button. The saved credential stays put. */
export const UNAVAILABLE_RESTORE_MESSAGE =
  "Could not reach the saved session. Check the network and try again.";

/**
 * Decide how to restore `stored`.
 *
 * A live session still loads the case and clips. After `/end`, those two reads
 * return 401/403/404 while upload status stays 200 inside the grace window —
 * that is upload-only mode. Any other failure (network, timeout, 5xx) stops
 * here as `unavailable`: credentials stay, and the console can retry. Grace
 * expiry and a security revocation both make upload status 401, and only then
 * are the saved credentials dropped.
 */
export async function restoreStudentSession(
  stored: StoredJoinedSession,
  probes: RestoreProbes,
): Promise<RestoredStudent> {
  try {
    const session = await probes.fetchSession(stored.sessionId);
    const clips = await probes.fetchClipManifest(session.case_id);
    return {
      kind: "live",
      sessionId: session.session_id,
      caseId: session.case_id,
      clips,
      accessToken: stored.accessToken,
    };
  } catch (cause) {
    // Only a refused live token is allowed to ask upload status. A network
    // error or 5xx on a live session must not look like an ended session.
    if (!refused(cause)) {
      return { kind: "unavailable" };
    }
  }

  try {
    await probes.fetchUploadStatus(stored.sessionId);
    return {
      kind: "upload-only",
      sessionId: stored.sessionId,
      accessToken: stored.accessToken,
    };
  } catch (cause) {
    if (refused(cause)) {
      probes.clearJoinedSession(stored.sessionId);
      return {
        kind: "rejected",
        message: "Saved session is no longer valid. Join again.",
      };
    }
    return { kind: "unavailable" };
  }
}
