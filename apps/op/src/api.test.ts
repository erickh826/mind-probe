import assert from "node:assert/strict";
import { test } from "node:test";

import { createOpClient, OpApiError } from "./api.ts";

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
