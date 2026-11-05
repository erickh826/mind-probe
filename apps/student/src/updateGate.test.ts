import assert from "node:assert/strict";
import test from "node:test";
import { canPromptStudentUpdate, type StudentUpdateGate } from "./updateGate.ts";

const base: StudentUpdateGate = {
  joined: true,
  recorderStatus: "idle",
  pendingCount: 0,
  bannerKind: "none",
};

test("never while recording, starting or stopping", () => {
  for (const recorderStatus of ["requesting", "recording", "stopping"] as const) {
    assert.equal(canPromptStudentUpdate({ ...base, recorderStatus }), false, recorderStatus);
    assert.equal(
      canPromptStudentUpdate({ ...base, recorderStatus, bannerKind: "safe" }),
      false,
      `${recorderStatus} with a stale safe banner`,
    );
  }
});

test("never while chunks are queued or finalize is unconfirmed", () => {
  assert.equal(canPromptStudentUpdate({ ...base, recorderStatus: "stopped", pendingCount: 3 }), false);
  assert.equal(canPromptStudentUpdate({ ...base, pendingCount: 1, bannerKind: "safe" }), false);
  for (const bannerKind of ["confirming", "uploading", "interrupted", "incomplete", "needs_operator"] as const) {
    assert.equal(canPromptStudentUpdate({ ...base, recorderStatus: "stopped", bannerKind }), false, bannerKind);
  }
});

test("stopped without a banner waits for safe", () => {
  assert.equal(canPromptStudentUpdate({ ...base, recorderStatus: "stopped" }), false);
  assert.equal(canPromptStudentUpdate({ ...base, recorderStatus: "stopped", bannerKind: "safe" }), true);
});

test("allowed before joining, before recording, and after a failed camera", () => {
  assert.equal(canPromptStudentUpdate({ ...base, joined: false }), true);
  assert.equal(canPromptStudentUpdate(base), true);
  assert.equal(canPromptStudentUpdate({ ...base, recorderStatus: "error" }), true);
  assert.equal(canPromptStudentUpdate({ ...base, recorderStatus: "unavailable" }), true);
});
