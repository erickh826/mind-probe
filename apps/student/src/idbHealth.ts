// IndexedDB diagnostics for the student recorder.
//
// A bare "IndexedDB error" does not say whether the device is out of space,
// the database version changed, or a transaction was aborted. Callers record
// the DOMException name and the step, and never delete the recording database.
// Unuploaded chunks have to survive a failed self-check and a failed write.

export const RECORDING_DB_NAME = "mindprobe-recording";
export const SELF_CHECK_DB_NAME = "mindprobe-idb-selfcheck";

const SELF_CHECK_STORE = "probe";
const PROBE_KEY = "blob";

export type IdbStep = "open_db" | "write_chunk" | "read_chunk" | "update_meta" | "finalize";

export interface IdbFailure {
  step: IdbStep;
  errorName: string;
  message: string;
}

export interface RecordingProblem {
  phase: "start" | "write";
  step: IdbStep | "camera" | "recorder" | "claim";
  errorName: string;
  message: string;
}

const STEP_LABEL: Record<IdbStep, string> = {
  open_db: "開啟資料庫",
  write_chunk: "寫入片段",
  read_chunk: "讀回片段",
  update_meta: "更新錄影狀態",
  finalize: "寫入結束標記",
};

/** DOMException name when one is present, otherwise a stable fallback. */
export function idbErrorName(cause: unknown): string {
  if (typeof DOMException !== "undefined" && cause instanceof DOMException && cause.name) {
    return cause.name;
  }
  if (cause instanceof Error && cause.name && cause.name !== "Error") {
    return cause.name;
  }
  if (cause && typeof cause === "object" && "name" in cause) {
    const name = (cause as { name: unknown }).name;
    if (typeof name === "string" && name.length > 0 && name !== "Error") {
      return name;
    }
  }
  return "UnknownError";
}

/** Traditional Chinese explanation. The name and step stay in the text. */
export function explainIdbFailure(errorName: string, step: IdbStep): string {
  const where = STEP_LABEL[step];
  switch (errorName) {
    case "QuotaExceededError":
      return `此裝置的儲存空間不足（${errorName}，${where}）。錄影不能安全保存。請通知 Wizard。不要清除網站資料，已保存的片段仍在。`;
    case "AbortError":
      return `瀏覽器中止了儲存操作（${errorName}，${where}）。已保存的片段仍在，新的片段不會再寫入。請通知 Wizard。不要清除網站資料。`;
    case "VersionError":
      return `瀏覽器的錄影資料庫版本不相容（${errorName}，${where}）。請通知 Wizard。不要清除網站資料。`;
    case "InvalidStateError":
      return `瀏覽器的錄影資料庫狀態無效（${errorName}，${where}）。請通知 Wizard。不要清除網站資料。`;
    case "UnknownError":
      return `瀏覽器無法使用本機儲存（${errorName}，${where}）。iPad 鎖定或儲存異常時可能出現這個錯誤。請通知 Wizard。不要清除網站資料。`;
    default:
      return `本機儲存失敗（${errorName}，${where}）。請通知 Wizard。不要清除網站資料，已保存的片段仍在。`;
  }
}

export function classifyIdbError(cause: unknown, step: IdbStep): IdbFailure {
  const errorName = idbErrorName(cause);
  return { step, errorName, message: explainIdbFailure(errorName, step) };
}

export function problemFromIdb(phase: RecordingProblem["phase"], failure: IdbFailure): RecordingProblem {
  return { phase, step: failure.step, errorName: failure.errorName, message: failure.message };
}

function rejectRequest(request: IDBRequest): unknown {
  return request.error ?? new DOMException("IndexedDB request failed", "UnknownError");
}

function openSelfCheckDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new DOMException("IndexedDB is not available", "InvalidStateError"));
      return;
    }
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(SELF_CHECK_DB_NAME, 1);
    } catch (cause) {
      reject(cause);
      return;
    }
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(SELF_CHECK_STORE)) {
        database.createObjectStore(SELF_CHECK_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(rejectRequest(request));
    request.onblocked = () => reject(new DOMException("IndexedDB open blocked", "InvalidStateError"));
  });
}

function deleteSelfCheckDb(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof indexedDB === "undefined") {
      resolve();
      return;
    }
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.deleteDatabase(SELF_CHECK_DB_NAME);
    } catch {
      resolve();
      return;
    }
    request.onsuccess = () => resolve();
    request.onerror = () => resolve();
    request.onblocked = () => resolve();
  });
}

async function runSelfCheck(): Promise<IdbFailure | null> {
  let database: IDBDatabase;
  try {
    database = await openSelfCheckDb();
  } catch (cause) {
    return classifyIdbError(cause, "open_db");
  }

  const blob = new Blob([new Uint8Array([1, 2, 3, 4])], { type: "application/octet-stream" });
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = database.transaction(SELF_CHECK_STORE, "readwrite");
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new DOMException("write failed", "UnknownError"));
      tx.onabort = () => reject(tx.error ?? new DOMException("write aborted", "AbortError"));
      try {
        tx.objectStore(SELF_CHECK_STORE).put(blob, PROBE_KEY);
      } catch (cause) {
        reject(cause);
      }
    });
  } catch (cause) {
    database.close();
    await deleteSelfCheckDb();
    return classifyIdbError(cause, "write_chunk");
  }

  try {
    const read = await new Promise<Blob>((resolve, reject) => {
      const tx = database.transaction(SELF_CHECK_STORE, "readonly");
      const request = tx.objectStore(SELF_CHECK_STORE).get(PROBE_KEY);
      request.onsuccess = () => {
        const value = request.result as Blob | undefined;
        if (!value || value.size !== blob.size) {
          reject(new DOMException("self-check blob did not round-trip", "UnknownError"));
          return;
        }
        resolve(value);
      };
      request.onerror = () => reject(rejectRequest(request));
      tx.onabort = () => reject(tx.error ?? new DOMException("read aborted", "AbortError"));
    });
    if (read.size !== blob.size) {
      return classifyIdbError(new DOMException("self-check blob mismatch", "UnknownError"), "read_chunk");
    }
  } catch (cause) {
    return classifyIdbError(cause, "read_chunk");
  } finally {
    database.close();
    await deleteSelfCheckDb();
  }
  return null;
}

let selfCheckInFlight: Promise<IdbFailure | null> | null = null;

/**
 * Open a throwaway database, write a small Blob, and read it back.
 * Deletes only `mindprobe-idb-selfcheck`, never the recording queue.
 */
export function selfCheckIndexedDb(): Promise<IdbFailure | null> {
  selfCheckInFlight ??= runSelfCheck().finally(() => {
    selfCheckInFlight = null;
  });
  return selfCheckInFlight;
}
