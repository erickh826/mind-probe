import "fake-indexeddb/auto";

import assert from "node:assert/strict";
import test from "node:test";

import {
  RECORDING_DB_NAME,
  SELF_CHECK_DB_NAME,
  classifyIdbError,
  explainIdbFailure,
  selfCheckIndexedDb,
} from "./idbHealth.ts";
import { openRecordingDb, writeMeta } from "./recordingDb.ts";
import { emptyMeta } from "./uploadQueue.ts";

const NAMES = ["QuotaExceededError", "AbortError", "UnknownError", "InvalidStateError", "VersionError"] as const;

test("classifies the IndexedDB error name and the step", () => {
  const steps = ["open_db", "write_chunk", "read_chunk", "update_meta", "finalize"] as const;
  for (const errorName of NAMES) {
    for (const step of steps) {
      const failure = classifyIdbError(new DOMException("boom", errorName), step);
      assert.equal(failure.errorName, errorName);
      assert.equal(failure.step, step);
      assert.match(failure.message, new RegExp(errorName));
      assert.match(failure.message, /不要清除網站資料/);
      assert.equal(explainIdbFailure(errorName, step), failure.message);
    }
  }
  const bare = classifyIdbError(new Error("IndexedDB request failed"), "open_db");
  assert.equal(bare.errorName, "UnknownError");
  assert.match(bare.message, /UnknownError/);
});

test("self-check round-trips a blob and does not delete the recording database", async () => {
  const recording = await openRecordingDb();
  await writeMeta(recording, emptyMeta("S-keep", 1));
  recording.close();

  const deleted: string[] = [];
  const original = indexedDB.deleteDatabase.bind(indexedDB);
  indexedDB.deleteDatabase = ((name: string) => {
    deleted.push(name);
    return original(name);
  }) as typeof indexedDB.deleteDatabase;
  try {
    assert.equal(await selfCheckIndexedDb(), null);
  } finally {
    indexedDB.deleteDatabase = original;
  }

  assert.deepEqual(deleted, [SELF_CHECK_DB_NAME]);
  assert.equal(deleted.includes(RECORDING_DB_NAME), false);

  const again = await openRecordingDb();
  const kept = await new Promise<unknown>((resolve, reject) => {
    const tx = again.transaction("recording_meta", "readonly");
    const request = tx.objectStore("recording_meta").get("S-keep");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  again.close();
  assert.equal((kept as { session_id: string }).session_id, "S-keep");
});

test("a failed open is reported and does not delete the recording database", async () => {
  const original = indexedDB.open.bind(indexedDB);
  indexedDB.open = ((name: string, version?: number) => {
    if (name === SELF_CHECK_DB_NAME) {
      const request = {
        error: new DOMException("upgrade blocked", "VersionError"),
        onupgradeneeded: null,
        onsuccess: null,
        onerror: null,
        onblocked: null,
      } as unknown as IDBOpenDBRequest;
      queueMicrotask(() => {
        request.onerror?.(new Event("error"));
      });
      return request;
    }
    return original(name, version);
  }) as typeof indexedDB.open;
  const deleted: string[] = [];
  const originalDelete = indexedDB.deleteDatabase.bind(indexedDB);
  indexedDB.deleteDatabase = ((name: string) => {
    deleted.push(name);
    return originalDelete(name);
  }) as typeof indexedDB.deleteDatabase;
  try {
    const failure = await selfCheckIndexedDb();
    assert.equal(failure?.errorName, "VersionError");
    assert.equal(failure?.step, "open_db");
    assert.match(failure?.message ?? "", /版本不相容/);
  } finally {
    indexedDB.open = original;
    indexedDB.deleteDatabase = originalDelete;
  }
  assert.equal(deleted.includes(RECORDING_DB_NAME), false);
});
