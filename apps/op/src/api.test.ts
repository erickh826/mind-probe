import assert from "node:assert/strict";
import { test } from "node:test";

import { createOpClient, OpApiError, operatorLogin } from "./api.ts";

interface Captured {
  url: string;
  init: RequestInit;
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function recordingClient(response: () => Response) {
  const calls: Captured[] = [];
  const client = createOpClient({
    baseUrl: "https://api.example",
    token: "op-secret",
    fetchImpl: async (input, init) => {
      calls.push({ url: String(input), init: init ?? {} });
      return response();
    },
  });
  return { client, calls };
}

test("the operator token travels only in the Authorization header", async () => {
  const { client, calls } = recordingClient(() => jsonResponse(200, { sessions: [] }));
  await client.listSessions();
  const [call] = calls;
  assert.equal(call.url, "https://api.example/sessions?limit=50");
  assert.ok(!call.url.includes("op-secret"));
  const headers = new Headers(call.init.headers);
  assert.equal(headers.get("Authorization"), "Bearer op-secret");
  assert.equal(call.init.credentials, "omit");
  assert.equal(call.init.cache, "no-store");
  assert.equal(call.init.referrerPolicy, "no-referrer");
});

test("older pages pass the cursor as an encoded before parameter", async () => {
  const { client, calls } = recordingClient(() => jsonResponse(200, { sessions: [], next_cursor: null }));
  await client.listSessions({ limit: 20, before: "WyIyMDI2Ii+=" });
  assert.equal(calls[0].url, "https://api.example/sessions?limit=20&before=WyIyMDI2Ii%2B%3D");
  await client.listSessions({ before: null });
  assert.equal(calls[1].url, "https://api.example/sessions?limit=50");
});

test("create posts JSON and session paths are encoded", async () => {
  const { client, calls } = recordingClient(() => jsonResponse(201, {}));
  await client.createSession("TRIAL-1", "CASE001");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ session_id: "TRIAL-1", case_id: "CASE001" }));
  assert.equal(new Headers(calls[0].init.headers).get("Content-Type"), "application/json");

  await client.reissueUploadCode("a/b");
  assert.equal(calls[1].url, "https://api.example/sessions/a%2Fb/upload-reissue");
  await client.regenerateReviewCodes("S 1");
  assert.equal(calls[2].url, "https://api.example/sessions/S%201/review-codes");
  await client.recordingStatus("S1");
  assert.equal(calls[3].url, "https://api.example/sessions/S1/recording/status");
});

test("join-code reissue posts the chosen roles and revoke flag", async () => {
  const { client, calls } = recordingClient(() =>
    jsonResponse(200, { session_id: "S 1", join_codes: {}, revoke_live: true, revoked_tokens: 1 }),
  );
  await client.reissueJoinCodes("S 1", ["student"], true);
  assert.equal(calls[0].url, "https://api.example/sessions/S%201/join-codes");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ roles: ["student"], revoke_live: true }));
});

test("errors carry the server detail, including the 410 object form", async () => {
  const conflict = recordingClient(() => jsonResponse(409, { detail: "upload_grace_expired" }));
  await assert.rejects(conflict.client.reissueUploadCode("S1"), (error: unknown) => {
    assert.ok(error instanceof OpApiError);
    assert.equal(error.status, 409);
    assert.equal(error.detail, "upload_grace_expired");
    return true;
  });

  const gone = recordingClient(() =>
    jsonResponse(410, { detail: { error: "recording_deleted", recording_state: "deleted" } }),
  );
  await assert.rejects(gone.client.recordingStatus("S1"), (error: unknown) => {
    assert.ok(error instanceof OpApiError);
    assert.equal(error.detail, "recording_deleted");
    return true;
  });

  const html = recordingClient(() => new Response("<html>bad gateway</html>", { status: 502 }));
  await assert.rejects(html.client.listCases(), (error: unknown) => {
    assert.ok(error instanceof OpApiError);
    assert.equal(error.status, 502);
    assert.equal(error.detail, "");
    return true;
  });
});

test("login sends the credentials in a JSON body only and returns the token", async () => {
  const calls: Captured[] = [];
  const login = await operatorLogin({
    baseUrl: "https://api.example",
    username: "admin",
    password: "pw secret",
    fetchImpl: async (input, init) => {
      calls.push({ url: String(input), init: init ?? {} });
      return jsonResponse(200, {
        access_token: "op-login-token",
        token_type: "operator",
        role: "operator",
        expires_at: 1_900_000_000,
      });
    },
  });
  assert.equal(login.access_token, "op-login-token");
  assert.equal(login.expires_at, 1_900_000_000);
  const [call] = calls;
  assert.equal(call.url, "https://api.example/op/login");
  assert.ok(!call.url.includes("admin") && !call.url.includes("pw"));
  assert.equal(call.init.method, "POST");
  assert.deepEqual(JSON.parse(String(call.init.body)), { username: "admin", password: "pw secret" });
  assert.equal(new Headers(call.init.headers).get("Authorization"), null);
  assert.equal(call.init.credentials, "omit");
  assert.equal(call.init.cache, "no-store");
  assert.equal(call.init.referrerPolicy, "no-referrer");
});

test("a refused login carries the server reason", async () => {
  await assert.rejects(
    operatorLogin({
      baseUrl: "https://api.example",
      username: "admin",
      password: "wrong",
      fetchImpl: async () => jsonResponse(401, { detail: "invalid_operator_credentials" }),
    }),
    (error: unknown) =>
      error instanceof OpApiError && error.status === 401 && error.detail === "invalid_operator_credentials",
  );
});

test("logout posts with the login token", async () => {
  const { client, calls } = recordingClient(() => jsonResponse(200, { status: "ok" }));
  await client.logout();
  assert.equal(calls[0].url, "https://api.example/op/logout");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(new Headers(calls[0].init.headers).get("Authorization"), "Bearer op-secret");
});
