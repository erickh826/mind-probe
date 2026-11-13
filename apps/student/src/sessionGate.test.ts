import assert from "node:assert/strict";
import test from "node:test";

import { planStudentLeave } from "./leaveSession.ts";
import {
  reconnectContinuesExistingTake,
  serverSessionInProgress,
  shouldAutoStartRecording,
  waitingForWizardCopy,
} from "./sessionGate.ts";
import type { CloseBanner } from "./uploadQueue.ts";

const ready = {
  restoredFromStorage: false,
  uploadOnly: false,
  admitted: true,
  sessionState: "active" as const,
  takeCheck: "free" as const,
  canStart: true,
  recorderStatus: "idle",
  alreadyAttempted: false,
};

test("recording starts only after the server session is active", () => {
  assert.equal(serverSessionInProgress("created"), false);
  assert.equal(serverSessionInProgress("starting"), false);
  assert.equal(serverSessionInProgress("active"), true);
  assert.equal(serverSessionInProgress("paused"), false);
  assert.equal(serverSessionInProgress("ended"), false);

  assert.equal(shouldAutoStartRecording(ready), true);
  assert.equal(shouldAutoStartRecording({ ...ready, sessionState: "created" }), false);
  assert.equal(shouldAutoStartRecording({ ...ready, sessionState: "starting" }), false);
  assert.equal(shouldAutoStartRecording({ ...ready, admitted: false }), false);
  assert.equal(shouldAutoStartRecording({ ...ready, restoredFromStorage: true }), false);
  assert.equal(shouldAutoStartRecording({ ...ready, recorderStatus: "recording" }), false);
  assert.match(waitingForWizardCopy("created", false) ?? "", /等待 Wizard/);
  assert.match(waitingForWizardCopy("starting", false) ?? "", /尚未開始/);
  assert.equal(waitingForWizardCopy("active", true), null);
});

test("reconnect continues a take instead of starting another", () => {
  assert.equal(reconnectContinuesExistingTake({ recorderStatus: "recording", hasLocalTake: false }), true);
  assert.equal(reconnectContinuesExistingTake({ recorderStatus: "idle", hasLocalTake: true }), true);
  assert.equal(reconnectContinuesExistingTake({ recorderStatus: "idle", hasLocalTake: false }), false);
  assert.equal(shouldAutoStartRecording({ ...ready, alreadyAttempted: true }), false);
});

test("leave never deletes chunks or reloads, and the three outcomes differ", () => {
  const none: CloseBanner = { kind: "none" };
  const recording = planStudentLeave({ banner: none, recorderStatus: "recording", pendingCount: 0 });
  const pending = planStudentLeave({
    banner: { kind: "uploading", pending: 2 },
    recorderStatus: "stopped",
    pendingCount: 2,
  });
  const complete = planStudentLeave({ banner: { kind: "safe" }, recorderStatus: "stopped", pendingCount: 0 });
  const incomplete = planStudentLeave({
    banner: { kind: "incomplete" },
    recorderStatus: "stopped",
    pendingCount: 0,
  });

  for (const plan of [recording, pending, complete, incomplete]) {
    assert.equal(plan.clearSavedJoin, true);
    assert.equal(plan.deleteLocalRecording, false);
    assert.equal(plan.reload, false);
    assert.match(plan.detail, /不會被刪除|不會刪除|不會被清除|清除這台裝置保存的加入資料/);
    assert.doesNotMatch(plan.detail, /重新整理頁面|強制重新載入|location\.reload/);
  }
  assert.match(recording.detail, /Stop REC/);
  assert.match(recording.detail, /結束場次/);
  assert.match(recording.detail, /不完整/);
  assert.equal(complete.buttonLabel, "回到加入頁");
  assert.match(complete.detail, /可以安全關閉/);
  assert.match(pending.detail, /補傳/);
  assert.match(incomplete.detail, /不完整/);
  assert.notEqual(complete.kind, incomplete.kind);
  assert.notEqual(complete.buttonLabel, "Stop REC");
  assert.notEqual(complete.buttonLabel, "結束場次");
});
