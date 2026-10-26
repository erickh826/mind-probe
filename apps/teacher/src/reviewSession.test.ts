import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  authorizedReviewFetch,
  clearReviewSession,
  forgetReviewMemoryForTests,
  readReviewSession,
  rememberReviewSession,
  resetReviewSessionStateForTests,
  type ReviewTokenResponse,
  type StoredReviewSession,
} from "../../../packages/shared/src/reviewSession.ts";
import { credentialFieldCopy } from "./sessionAuthMode.ts";

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

test("review credentials round-trip through memory and sessionStorage", () => {
  installStorage();
  rememberReviewSession(stored);
  assert.deepEqual(readReviewSession(), stored);
  forgetReviewMemoryForTests();
  assert.deepEqual(readReviewSession(), stored);
  clearReviewSession();
  forgetReviewMemoryForTests();
  assert.equal(readReviewSession(), null);
  assert.equal(memory.size, 0);
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

test("a revoked review token is not refreshed", async () => {
  installStorage();
  rememberReviewSession(stored);
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
  assert.equal(response.status, 401);
  assert.equal(refreshes, 0);
  assert.equal(readReviewSession()?.refreshToken, "refresh-old");
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
