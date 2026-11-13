import assert from "node:assert/strict";
import test from "node:test";

import { planWizardLeave } from "./leaveSession.ts";

test("wizard leave clears the saved join and does not end the session or reload", () => {
  for (const ended of [false, true]) {
    const plan = planWizardLeave(ended);
    assert.equal(plan.clearSavedJoin, true);
    assert.equal(plan.reload, false);
    assert.equal(plan.endsSession, false);
    assert.equal(plan.buttonLabel, "離開場次");
    assert.notEqual(plan.buttonLabel, "結束場次");
    assert.notEqual(plan.buttonLabel, "Stop REC");
    assert.match(plan.detail, /結束場次/);
    assert.match(plan.detail, /加入/);
  }
  assert.match(planWizardLeave(false).title, /不結束場次/);
  assert.match(planWizardLeave(true).detail, /已經結束/);
});
