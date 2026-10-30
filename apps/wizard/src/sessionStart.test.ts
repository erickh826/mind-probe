import assert from "node:assert/strict";
import test from "node:test";

import {
  START_OFFLINE_MESSAGE,
  authoritativeStartCheckDelayMs,
  canDispatchSessionStart,
  startControl,
  startRejectionMessage,
} from "./sessionStart.ts";

const created = {
  connected: true,
  sessionState: "created" as const,
  sessionEnded: false,
  startInFlight: false,
};

test("start is enabled only for a connected created session", () => {
  const control = startControl(created);
  assert.equal(control.enabled, true);
  assert.equal(control.pending, false);
  assert.equal(control.label, "開始場次");
  assert.equal(canDispatchSessionStart(created), true);
});

test("disconnected, in-flight, and ended states cannot start again", () => {
  const offline = startControl({ ...created, connected: false });
  assert.equal(offline.enabled, false);
  assert.equal(offline.pending, false);
  assert.match(offline.title, /連線中斷/);

  const pending = startControl({ ...created, startInFlight: true });
  assert.equal(pending.enabled, false);
  assert.equal(pending.pending, true);
  assert.equal(pending.label, "開始中…");
  assert.equal(
    canDispatchSessionStart({ ...created, startInFlight: true }),
    false,
  );

  for (const sessionState of ["starting", "active", "paused", "ended"] as const) {
    const control = startControl({ ...created, sessionState });
    assert.equal(control.enabled, false, sessionState);
    assert.equal(control.pending, false, sessionState);
    assert.equal(canDispatchSessionStart({ ...created, sessionState }), false);
  }

  const endedLocally = startControl({ ...created, sessionEnded: true });
  assert.equal(endedLocally.enabled, false);
  assert.equal(endedLocally.title, "場次已結束");

  const blocked = startControl({ ...created, blocked: true });
  assert.equal(blocked.enabled, false);
  assert.match(blocked.title, /目前的場次操作/);
});

test("pause tells the operator to resume instead of starting", () => {
  const paused = startControl({ ...created, sessionState: "paused" });
  assert.match(paused.title, /恢復/);
});

test("rejection and offline copy stay specific", () => {
  assert.match(startRejectionMessage("invalid_lifecycle_transition"), /目前狀態/);
  assert.match(startRejectionMessage("session_start_requires_wizard"), /Wizard/);
  assert.match(startRejectionMessage(null), /未知錯誤/);
  assert.match(START_OFFLINE_MESSAGE, /連線中斷/);
});

test("scheduled-start check waits on the server clock", () => {
  assert.equal(authoritativeStartCheckDelayMs(5_000, 1_000, null), 2_000);
  assert.equal(authoritativeStartCheckDelayMs(5_000, 1_000, 3_500), 500);
  assert.equal(authoritativeStartCheckDelayMs(5_000, 4_000, 2_000), 0);
});
