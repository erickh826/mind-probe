import assert from "node:assert/strict";
import test from "node:test";
import {
  classifyClose,
  isRetryableClose,
  SOCKET_BACKOFF_MS,
  SocketLifecycle,
} from "./socketLifecycle.ts";

test("close codes map to what the client should do", () => {
  assert.equal(classifyClose(4429, "role_already_connected"), "occupied");
  assert.equal(classifyClose(1013, "session_state_unavailable"), "unavailable");
  assert.equal(classifyClose(4409, "role_connection_replaced"), "replaced");
  assert.equal(classifyClose(4401, "token_revoked"), "revoked");
  assert.equal(classifyClose(4401, "session_ended"), "ended");
  assert.equal(classifyClose(4401, "expired_session_token"), "refused");
  assert.equal(classifyClose(4403, "role_token_mismatch"), "refused");
  assert.equal(classifyClose(4404, "session_not_found"), "refused");
  assert.equal(classifyClose(4400, "invalid_role"), "refused");
  assert.equal(classifyClose(1006, ""), "lost");
  assert.equal(classifyClose(1000, ""), "lost");

  assert.equal(isRetryableClose("occupied"), true);
  assert.equal(isRetryableClose("unavailable"), true);
  assert.equal(isRetryableClose("lost"), true);
  for (const kind of ["replaced", "revoked", "ended", "refused"] as const) {
    assert.equal(isRetryableClose(kind), false, kind);
  }
});

test("an open transport is not admission; the first snapshot is", () => {
  const life = new SocketLifecycle();
  const { id, status } = life.begin();
  assert.equal(status, "connecting");
  assert.deepEqual(life.opened(id), { sendHello: true });
  assert.equal(life.isAdmitted(id), false);

  assert.deepEqual(life.snapshot(id), {
    status: "open",
    closedReason: null,
    notice: null,
    admitted: true,
  });
  assert.equal(life.isAdmitted(id), true);
  // Later snapshots (revision bumps) do not admit again.
  assert.deepEqual(life.snapshot(id), {});
});

test("repeated 4429 refusals keep backing off instead of resetting on open", () => {
  // The server accepts before it refuses, so every attempt fires onopen.
  const life = new SocketLifecycle();
  const delays: number[] = [];
  for (let i = 0; i < 7; i += 1) {
    const { id } = life.begin();
    life.opened(id);
    const effects = life.closed(id, 4429, "role_already_connected");
    assert.equal(effects.status, "reconnecting");
    assert.equal(effects.notice, "occupied");
    assert.equal(effects.stop, undefined);
    assert.equal(effects.closedReason, undefined);
    delays.push(effects.reconnectInMs ?? -1);
  }
  assert.deepEqual(delays, [1_000, 2_000, 4_000, 8_000, 15_000, 15_000, 15_000]);
});

test("1013 backs off the same way and never admits", () => {
  const life = new SocketLifecycle();
  const first = life.begin();
  life.opened(first.id);
  assert.equal(life.closed(first.id, 1013, "session_state_unavailable").reconnectInMs, 1_000);
  const second = life.begin();
  life.opened(second.id);
  const effects = life.closed(second.id, 1013, "session_state_unavailable");
  assert.equal(effects.reconnectInMs, 2_000);
  assert.equal(effects.notice, "unavailable");
  assert.equal(life.isAdmitted(second.id), false);
});

test("admission resets the backoff; a later drop starts over", () => {
  const life = new SocketLifecycle();
  for (let i = 0; i < 3; i += 1) {
    const { id } = life.begin();
    life.opened(id);
    life.closed(id, 4429, "role_already_connected");
  }
  const { id } = life.begin();
  life.opened(id);
  life.snapshot(id);
  const dropped = life.closed(id, 1006, "");
  assert.equal(dropped.reconnectInMs, SOCKET_BACKOFF_MS[0]);
  assert.equal(dropped.notice, "lost");
  assert.equal(life.begin().status, "reconnecting");
});

test("final closes stop reconnecting and carry their reason", () => {
  for (const [code, reason, kind] of [
    [4409, "role_connection_replaced", "replaced"],
    [4401, "token_revoked", "revoked"],
    [4401, "session_ended", "ended"],
    [4404, "session_not_found", "refused"],
  ] as const) {
    const life = new SocketLifecycle();
    const { id } = life.begin();
    life.opened(id);
    life.snapshot(id);
    assert.deepEqual(life.closed(id, code, reason), {
      status: "closed",
      closedReason: reason,
      notice: kind,
      stop: true,
    });
    const next = life.begin();
    assert.equal(life.isCurrent(next.id), false, "stopped lifecycles admit nothing");
    assert.deepEqual(life.snapshot(next.id), {});
  }
});

test("callbacks from a socket that is no longer current are ignored", () => {
  const life = new SocketLifecycle();
  const old = life.begin();
  life.opened(old.id);
  life.snapshot(old.id);
  // Heartbeat timeout: the old socket is detached and a new one opened.
  const timeout = life.timedOut(old.id);
  assert.equal(timeout.detach, true);
  assert.equal(timeout.reconnectInMs, SOCKET_BACKOFF_MS[0]);
  const fresh = life.begin();
  life.opened(fresh.id);

  // The old socket's late events must not admit, close, or schedule anything.
  assert.deepEqual(life.snapshot(old.id), {});
  assert.deepEqual(life.closed(old.id, 4409, "role_connection_replaced"), {});
  assert.deepEqual(life.timedOut(old.id), {});
  assert.deepEqual(life.opened(old.id), {});
  assert.equal(life.isAdmitted(fresh.id), false);

  assert.equal(life.snapshot(fresh.id).admitted, true);
});

test("an admission timeout drops a transport that never got its snapshot", () => {
  const life = new SocketLifecycle();
  const { id } = life.begin();
  life.opened(id);
  const effects = life.timedOut(id);
  assert.equal(effects.detach, true);
  assert.equal(effects.status, "reconnecting");
  assert.equal(effects.notice, "lost");
  assert.equal(effects.reconnectInMs, SOCKET_BACKOFF_MS[0]);
});

test("dispose ignores everything that arrives afterwards", () => {
  const life = new SocketLifecycle();
  const { id } = life.begin();
  life.opened(id);
  life.dispose();
  assert.deepEqual(life.snapshot(id), {});
  assert.deepEqual(life.closed(id, 1006, ""), {});
});
