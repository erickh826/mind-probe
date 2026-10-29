import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addFirstReads,
  appendPage,
  attentionFlags,
  createGeneration,
  defaultSessionId,
  describeProgress,
  errorMessage,
  formatSeconds,
  graceRemainingSeconds,
  labelFor,
  nextReads,
  parseServerTime,
  RECORDING_LABELS,
  sessionIdProblem,
} from "./opLogic.ts";

test("default session id follows the TRIAL-date-time naming and is a valid id", () => {
  const id = defaultSessionId(new Date(2026, 9, 28, 9, 41, 5));
  assert.equal(id, "TRIAL-20261028-094105");
  assert.equal(sessionIdProblem(id), null);
});

test("session ids the recording store would reject are flagged before create", () => {
  assert.notEqual(sessionIdProblem(""), null);
  assert.notEqual(sessionIdProblem("-leading"), null);
  assert.notEqual(sessionIdProblem("has space"), null);
  assert.notEqual(sessionIdProblem("../x"), null);
  assert.notEqual(sessionIdProblem("a".repeat(129)), null);
  assert.equal(sessionIdProblem("S_001-b"), null);
});

test("grace remaining counts down and stops at zero", () => {
  const end = "2026-10-28T10:00:00+00:00";
  const endMs = Date.parse(end);
  assert.equal(graceRemainingSeconds(null, endMs), null);
  assert.equal(graceRemainingSeconds(end, endMs - 90_500), 90);
  assert.equal(graceRemainingSeconds(end, endMs + 5_000), 0);
  assert.equal(graceRemainingSeconds("not a date", endMs), null);
});

test("format seconds", () => {
  assert.equal(formatSeconds(null), "—");
  assert.equal(formatSeconds(42), "42 秒");
  assert.equal(formatSeconds(600), "10 分");
  assert.equal(formatSeconds(125), "2 分 5 秒");
  assert.equal(formatSeconds(3_900), "1 時 5 分");
});

test("progress needs two reads before it says anything about the recorder", () => {
  const first = { storedSequences: 10, readAtMs: 1_000 };
  assert.match(describeProgress(undefined, first), /第一次讀取/);
  assert.match(describeProgress(first, first), /第一次讀取/);
  assert.match(describeProgress(first, { storedSequences: 13, readAtMs: 31_000 }), /30 秒內新增 3 個片段/);
  assert.match(describeProgress(first, { storedSequences: 10, readAtMs: 31_000 }), /30 秒內沒有新片段/);
  assert.match(describeProgress(first, { storedSequences: 0, readAtMs: 31_000 }), /少 10 個/);
});

test("attention flags", () => {
  const base = {
    status: "active",
    recording_state: "recording",
    finalize_state: "none",
    incomplete_reason: null,
    needs_operator: false,
  };
  assert.deepEqual(attentionFlags(base), []);
  assert.deepEqual(
    attentionFlags({
      ...base,
      recording_state: "incomplete",
      finalize_state: "incomplete",
      incomplete_reason: "recorder_lost",
      needs_operator: true,
    }),
    ["需要 OP 處理", "錄影中斷"],
  );
  assert.deepEqual(attentionFlags({ ...base, recording_state: "incomplete" }), ["錄影不完整"]);
  assert.deepEqual(attentionFlags({ ...base, recording_state: "transcode_failed" }), ["轉檔失敗"]);
  assert.deepEqual(attentionFlags({ ...base, status: "ended", recording_state: null }), [
    "已結束但沒有錄影",
  ]);
});

test("server reasons map to operator copy, unknown ones pass through", () => {
  assert.match(errorMessage(401, "invalid_operator_token"), /Operator token/);
  assert.match(errorMessage(409, "upload_grace_expired"), /寬限期已過/);
  assert.match(errorMessage(409, "session already exists: S-1"), /已經存在/);
  assert.equal(errorMessage(500, "boom"), "boom（HTTP 500）");
  assert.equal(errorMessage(502, ""), "HTTP 502");
  assert.match(errorMessage(0, ""), /連不上伺服器/);
});

test("labels fall back to the raw value", () => {
  assert.equal(labelFor(RECORDING_LABELS, "complete"), "完整");
  assert.equal(labelFor(RECORDING_LABELS, "mystery"), "mystery");
  assert.equal(labelFor(RECORDING_LABELS, null), "—");
});

test("naive server timestamps are read as UTC", () => {
  assert.equal(parseServerTime("2026-10-28T09:41:05")?.toISOString(), "2026-10-28T09:41:05.000Z");
  assert.equal(parseServerTime("2026-10-28T09:41:05+00:00")?.toISOString(), "2026-10-28T09:41:05.000Z");
  assert.equal(parseServerTime("2026-10-28T17:41:05+08:00")?.toISOString(), "2026-10-28T09:41:05.000Z");
  assert.equal(parseServerTime(null), null);
  assert.equal(parseServerTime("garbage"), null);
});

test("a ticket taken before sign-out stays invalid after signing back in", () => {
  const auth = createGeneration();
  auth.advance(); // signed in
  const pending = auth.ticket(); // a reissue request starts
  assert.equal(pending(), true);
  auth.advance(); // signed out
  assert.equal(pending(), false);
  const fresh = auth.advance(); // signed in again, same token
  assert.equal(pending(), false);
  assert.equal(fresh(), true);
});

test("only the latest list request may apply its response", () => {
  const list = createGeneration();
  const first = list.advance();
  const second = list.advance();
  assert.equal(first(), false);
  assert.equal(second(), true);
});

test("an older page appends without repeating rows already shown", () => {
  const shown = [{ session_id: "S-3" }, { session_id: "S-2" }];
  const older = [{ session_id: "S-2" }, { session_id: "S-1" }];
  assert.deepEqual(
    appendPage(shown, older).map((row) => row.session_id),
    ["S-3", "S-2", "S-1"],
  );
});

test("a full re-read shifts reads; an older page only adds first reads", () => {
  const first = nextReads({}, [{ session_id: "S-2", stored_sequences: 4 }], 1_000);
  const reread = nextReads(first, [{ session_id: "S-2", stored_sequences: 7 }], 31_000);
  assert.deepEqual(reread["S-2"], {
    previous: { storedSequences: 4, readAtMs: 1_000 },
    current: { storedSequences: 7, readAtMs: 31_000 },
  });

  const paged = addFirstReads(
    reread,
    [
      { session_id: "S-2", stored_sequences: 9 },
      { session_id: "S-1", stored_sequences: 2 },
    ],
    40_000,
  );
  assert.deepEqual(paged["S-2"], reread["S-2"]);
  assert.deepEqual(paged["S-1"], { current: { storedSequences: 2, readAtMs: 40_000 } });
});
