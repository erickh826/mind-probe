import assert from "node:assert/strict";
import test from "node:test";

import { recordingAlertFromNotice, type RecordingFailureNotice } from "./recordingAlert.ts";

test("a snapshot restores the recording-failure banner and null clears it", () => {
  const notice: RecordingFailureNotice = {
    phase: "write",
    step: "write_chunk",
    error_name: "QuotaExceededError",
    message: "此裝置的儲存空間不足",
  };
  const banner = recordingAlertFromNotice(notice);
  assert.match(banner ?? "", /儲存空間不足/);
  assert.match(banner ?? "", /QuotaExceededError/);
  assert.match(banner ?? "", /write_chunk/);
  assert.equal(recordingAlertFromNotice(null), null);
  assert.equal(recordingAlertFromNotice(undefined), null);
  assert.equal(recordingAlertFromNotice({}), null);
});
