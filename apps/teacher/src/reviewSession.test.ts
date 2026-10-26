import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { readAccessToken, rememberAccessToken } from "../../../packages/shared/src/sessionAuth.ts";
import {
  authorizedReviewFetch,
  clearReviewSession,
  forgetReviewMemoryForTests,
  parkReviewRefreshForReload,
  readReviewSession,
  rememberReviewSession,
  resetReviewSessionStateForTests,
  setReviewNavigationTypeForTests,
  subscribeReviewSessionDrop,
  unparkReviewRefresh,
  type ReviewTokenResponse,
  type StoredReviewSession,
} from "../../../packages/shared/src/reviewSession.ts";
import { reviewErrorMessage, shouldReturnToReviewLogin } from "./reviewErrors.ts";
import { credentialFieldCopy, readAuthMode, rememberAuthMode } from "./sessionAuthMode.ts";

const REVIEW_SESSION_KEY = "mindprobe.review_session";

const memory = new Map<string, string>();

function installStorage(): void {
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => memory.get(key) ?? null,
      setItem: (key: string, value: string) => {
        memory.set(key, value);
      },
      removeItem: (key: string) => {
        memory.delete(key);
      },
    },
  });
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function detailOf(response: Response): Promise<string> {
  const body = (await response.clone().json()) as { detail?: unknown };
  return typeof body.detail === "string" ? body.detail : "";
}

const stored: StoredReviewSession = {
  sessionId: "S-1",
  role: "teacher",
  accessToken: "access-old",
  refreshToken: "refresh-old",
  expiresAt: 1_700_000_000,
};

afterEach(() => {
  memory.clear();
  resetReviewSessionStateForTests();
});

test("review credentials stay in memory while sessionStorage omits the refresh token", () => {
  installStorage();
  rememberReviewSession(stored);
  assert.deepEqual(readReviewSession(), stored);
  const parked = JSON.parse(memory.get(REVIEW_SESSION_KEY) ?? "{}") as { refreshToken?: string };
  assert.equal(parked.refreshToken, "");
  forgetReviewMemoryForTests();
  setReviewNavigationTypeForTests("back_forward");
  const duplicated = readReviewSession();
  assert.equal(duplicated?.accessToken, stored.accessToken);
  assert.equal(duplicated?.refreshToken, "");
  clearReviewSession();
  forgetReviewMemoryForTests();
  assert.equal(readReviewSession(), null);
  assert.equal(memory.size, 0);
});

test("a same-tab reload claims the parked refresh token and strips it again", () => {
  installStorage();
  setReviewNavigationTypeForTests("reload");
  rememberReviewSession(stored);
  parkReviewRefreshForReload();
  const parked = JSON.parse(memory.get(REVIEW_SESSION_KEY) ?? "{}") as { refreshToken?: string };
  assert.equal(parked.refreshToken, "refresh-old");
  assert.equal(memory.get("mindprobe.review_reload"), "1");
  forgetReviewMemoryForTests();
  assert.equal(readReviewSession()?.refreshToken, "refresh-old");
  const afterClaim = JSON.parse(memory.get(REVIEW_SESSION_KEY) ?? "{}") as { refreshToken?: string };
  assert.equal(afterClaim.refreshToken, "");
  assert.equal(memory.has("mindprobe.review_reload"), false);
});

test("a duplicated tab discards a parked refresh token", () => {
  installStorage();
  rememberReviewSession(stored);
  parkReviewRefreshForReload();
  forgetReviewMemoryForTests();
  setReviewNavigationTypeForTests("back_forward");
  const duplicated = readReviewSession();
  assert.equal(duplicated?.accessToken, stored.accessToken);
  assert.equal(duplicated?.refreshToken, "");
  const leftBehind = JSON.parse(memory.get(REVIEW_SESSION_KEY) ?? "{}") as { refreshToken?: string };
  assert.equal(leftBehind.refreshToken, "");
  assert.equal(memory.has("mindprobe.review_reload"), false);
});

test("an unknown navigation type can claim a parked refresh token from the reload marker", () => {
  installStorage();
  setReviewNavigationTypeForTests("");
  rememberReviewSession(stored);
  parkReviewRefreshForReload();
  forgetReviewMemoryForTests();
  assert.equal(readReviewSession()?.refreshToken, "refresh-old");
});

test("bfcache restore takes the refresh token back out of sessionStorage", () => {
  installStorage();
  rememberReviewSession(stored);
  parkReviewRefreshForReload();
  unparkReviewRefresh();
  const visible = JSON.parse(memory.get(REVIEW_SESSION_KEY) ?? "{}") as { refreshToken?: string };
  assert.equal(visible.refreshToken, "");
  assert.equal(readReviewSession()?.refreshToken, "refresh-old");
  assert.equal(memory.has("mindprobe.review_reload"), false);
});

test("review-code mode is a distinct login field", () => {
  assert.deepEqual(credentialFieldCopy("review_code"), {
    legend: "Review code",
    placeholder: "Review code",
  });
  assert.equal(credentialFieldCopy("join_code").legend, "Teacher code");
  assert.equal(credentialFieldCopy("bearer").legend, "Token");
});

test("401 expired refreshes once and retries with the new access token", async () => {
  installStorage();
  rememberReviewSession(stored);
  const seen: string[] = [];
  let refreshes = 0;
  const response = await authorizedReviewFetch({
    requestedToken: stored.accessToken,
    sessionId: stored.sessionId,
    perform: async (accessToken) => {
      seen.push(accessToken);
      if (accessToken === "access-old") {
        return jsonResponse(401, { detail: "expired_session_token" });
      }
      return jsonResponse(200, { ok: true });
    },
    refresh: async (refreshToken) => {
      refreshes += 1;
      assert.equal(refreshToken, "refresh-old");
      const body: ReviewTokenResponse = {
        role: "teacher",
        access_token: "access-new",
        refresh_token: "refresh-new",
        expires_at: 1_700_001_800,
      };
      return body;
    },
    readDetail: detailOf,
  });
  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["access-old", "access-new"]);
  assert.equal(refreshes, 1);
  assert.equal(readReviewSession()?.accessToken, "access-new");
  assert.equal(readReviewSession()?.refreshToken, "refresh-new");
});

test("parallel 401s share one refresh so the old refresh is not reused", async () => {
  installStorage();
  rememberReviewSession(stored);
  let refreshes = 0;
  const refresh = async (): Promise<ReviewTokenResponse> => {
    refreshes += 1;
    await new Promise((resolve) => setTimeout(resolve, 15));
    return {
      role: "observer",
      access_token: "access-new",
      refresh_token: "refresh-new",
      expires_at: 1_700_001_800,
    };
  };
  const perform = async (accessToken: string) => {
    if (accessToken === "access-old") {
      return jsonResponse(401, { detail: "expired" });
    }
    return jsonResponse(200, { token: accessToken });
  };
  const [first, second] = await Promise.all([
    authorizedReviewFetch({
      requestedToken: "access-old",
      sessionId: "S-1",
      perform,
      refresh,
      readDetail: detailOf,
    }),
    authorizedReviewFetch({
      requestedToken: "access-old",
      sessionId: "S-1",
      perform,
      refresh,
      readDetail: detailOf,
    }),
  ]);
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal(refreshes, 1);
  assert.equal(readReviewSession()?.role, "observer");
});

test("a revoked review token is not refreshed and stored credentials are cleared", async () => {
  installStorage();
  rememberReviewSession(stored);
  rememberAccessToken(stored.sessionId, stored.accessToken);
  const dropped: string[] = [];
  const unsubscribe = subscribeReviewSessionDrop((detail) => {
    dropped.push(detail);
  });
  let refreshes = 0;
  const response = await authorizedReviewFetch({
    requestedToken: stored.accessToken,
    sessionId: stored.sessionId,
    perform: async () => jsonResponse(401, { detail: "revoked_session_token" }),
    refresh: async () => {
      refreshes += 1;
      throw new Error("should not refresh");
    },
    readDetail: detailOf,
  });
  unsubscribe();
  assert.equal(response.status, 401);
  assert.equal(refreshes, 0);
  assert.equal(readReviewSession(), null);
  assert.equal(readAccessToken(stored.sessionId), null);
  assert.deepEqual(dropped, ["revoked_session_token"]);
  assert.equal(memory.has(REVIEW_SESSION_KEY), false);
});

test("a forbidden review response does not clear the session or refresh", async () => {
  installStorage();
  rememberReviewSession(stored);
  let refreshes = 0;
  const response = await authorizedReviewFetch({
    requestedToken: stored.accessToken,
    sessionId: stored.sessionId,
    perform: async () => jsonResponse(403, { detail: "role 'observer' may not use this endpoint" }),
    refresh: async () => {
      refreshes += 1;
      throw new Error("should not refresh");
    },
    readDetail: detailOf,
  });
  assert.equal(response.status, 403);
  assert.equal(refreshes, 0);
  assert.equal(readReviewSession()?.refreshToken, "refresh-old");
});

test("an expired access token without a refresh token is not sent to review-refresh", async () => {
  installStorage();
  rememberReviewSession(stored);
  forgetReviewMemoryForTests();
  setReviewNavigationTypeForTests("navigate");
  assert.equal(readReviewSession()?.refreshToken, "");
  let refreshes = 0;
  const response = await authorizedReviewFetch({
    requestedToken: stored.accessToken,
    sessionId: stored.sessionId,
    perform: async () => jsonResponse(401, { detail: "expired_session_token" }),
    refresh: async () => {
      refreshes += 1;
      throw new Error("should not refresh");
    },
    readDetail: detailOf,
  });
  assert.equal(response.status, 401);
  assert.equal(refreshes, 0);
  assert.equal(readReviewSession(), null);
});

test("a failed refresh clears the review session", async () => {
  installStorage();
  rememberReviewSession(stored);
  const response = authorizedReviewFetch({
    requestedToken: stored.accessToken,
    sessionId: stored.sessionId,
    perform: async () => jsonResponse(401, { detail: "expired_session_token" }),
    refresh: async () => {
      throw Object.assign(new Error("reused"), { detail: "refresh_reused" });
    },
    readDetail: detailOf,
  });
  await assert.rejects(response, /reused/);
  assert.equal(readReviewSession(), null);
});

test("common review errors become short sign-in messages", () => {
  assert.equal(reviewErrorMessage("rate_limited"), "Too many attempts. Wait a minute and try again.");
  assert.equal(reviewErrorMessage("invalid_review_code"), "That review code is not valid.");
  assert.match(reviewErrorMessage("revoked_session_token"), /revoked/i);
  assert.equal(reviewErrorMessage("not_a_known_code"), "not_a_known_code");
  assert.equal(shouldReturnToReviewLogin("revoked_session_token"), true);
  assert.equal(shouldReturnToReviewLogin("rate_limited"), true);
  assert.equal(shouldReturnToReviewLogin("token_type_mismatch"), false);
});

test("the teacher auth mode survives in sessionStorage without the review secret", () => {
  installStorage();
  rememberAuthMode("review_code");
  assert.equal(readAuthMode(), "review_code");
  assert.equal(memory.has(REVIEW_SESSION_KEY), false);
});

test("a live token is not sent through review refresh", async () => {
  installStorage();
  rememberReviewSession(stored);
  const seen: string[] = [];
  const response = await authorizedReviewFetch({
    requestedToken: "live-token",
    sessionId: stored.sessionId,
    perform: async (accessToken) => {
      seen.push(accessToken);
      return jsonResponse(200, { ok: true });
    },
    refresh: async () => {
      throw new Error("should not refresh");
    },
    readDetail: detailOf,
  });
  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["live-token"]);
});
