// IndexedDB upload queue (ADR-0005 §6.7).
//
// `chunks` and `recording_meta` share one readwrite transaction when a slice
// is stored, so `next_sequence` and the blob commit together. The follow-up
// writes run inside the get's success handler: yielding between IndexedDB
// requests lets the transaction commit early. Deleting an ACKed blob opens
// only the chunk store.

import {
  buildStoredChunk,
  emptyMeta,
  shouldDiscardMeta,
  type RecordingMeta,
} from "./uploadQueue";

const DB_NAME = "mindprobe-recording";
const DB_VERSION = 1;
const CHUNK_STORE = "chunks";
const META_STORE = "recording_meta";

export interface PersistedChunk {
  session_id: string;
  sequence: number;
  start_ms: number;
  end_ms: number;
  checksum: string;
  blob: Blob;
  attempts: number;
  next_attempt_at: number;
}

function fail(reject: (reason: unknown) => void, error: DOMException | null): void {
  reject(error ?? new Error("IndexedDB request failed"));
}

export function openRecordingDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(CHUNK_STORE)) {
        const chunks = db.createObjectStore(CHUNK_STORE, {
          keyPath: ["session_id", "sequence"],
        });
        chunks.createIndex("by_session", "session_id", { unique: false });
      }
      if (!db.objectStoreNames.contains(META_STORE)) {
        db.createObjectStore(META_STORE, { keyPath: "session_id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => fail(reject, request.error);
  });
}

export function readMeta(db: IDBDatabase, sessionId: string): Promise<RecordingMeta | null> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(META_STORE, "readonly");
    const request = tx.objectStore(META_STORE).get(sessionId);
    request.onsuccess = () => resolve((request.result as RecordingMeta | undefined) ?? null);
    request.onerror = () => fail(reject, request.error);
  });
}

export function writeMeta(db: IDBDatabase, meta: RecordingMeta): Promise<void> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(META_STORE, "readwrite");
    tx.oncomplete = () => resolve();
    tx.onerror = () => fail(reject, tx.error);
    tx.onabort = () => fail(reject, tx.error);
    tx.objectStore(META_STORE).put(meta);
  });
}

export function storeChunkAndBumpSequence(
  db: IDBDatabase,
  sessionId: string,
  blob: Blob,
  timing: { end_ms: number; checksum: string; now: number },
): Promise<{ sequence: number; meta: RecordingMeta }> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction([CHUNK_STORE, META_STORE], "readwrite");
    const metaStore = tx.objectStore(META_STORE);
    const chunkStore = tx.objectStore(CHUNK_STORE);
    const getReq = metaStore.get(sessionId);
    getReq.onerror = () => fail(reject, getReq.error);
    tx.onerror = () => fail(reject, tx.error);
    tx.onabort = () => fail(reject, tx.error);
    getReq.onsuccess = () => {
      const existing = getReq.result as RecordingMeta | undefined;
      const meta = existing ?? emptyMeta(sessionId, timing.now);
      const built = buildStoredChunk(meta, {
        end_ms: timing.end_ms,
        checksum: timing.checksum,
        byte_length: blob.size,
      });
      const row: PersistedChunk = {
        session_id: sessionId,
        sequence: built.sequence,
        start_ms: built.chunk.start_ms,
        end_ms: built.chunk.end_ms,
        checksum: timing.checksum,
        blob,
        attempts: 0,
        next_attempt_at: 0,
      };
      chunkStore.put(row);
      metaStore.put(built.meta);
      tx.oncomplete = () => resolve({ sequence: built.sequence, meta: built.meta });
    };
  });
}

export function listChunks(db: IDBDatabase, sessionId: string): Promise<PersistedChunk[]> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(CHUNK_STORE, "readonly");
    const request = tx.objectStore(CHUNK_STORE).index("by_session").getAll(sessionId);
    request.onsuccess = () => {
      const rows = (request.result as PersistedChunk[]) ?? [];
      resolve(rows.sort((a, b) => a.sequence - b.sequence));
    };
    request.onerror = () => fail(reject, request.error);
  });
}

/** Delete one ACKed blob. `recording_meta.next_sequence` is not touched. */
export function deleteChunk(
  db: IDBDatabase,
  sessionId: string,
  sequence: number,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(CHUNK_STORE, "readwrite");
    tx.oncomplete = () => resolve();
    tx.onerror = () => fail(reject, tx.error);
    tx.onabort = () => fail(reject, tx.error);
    tx.objectStore(CHUNK_STORE).delete([sessionId, sequence]);
  });
}

export function writeChunk(db: IDBDatabase, chunk: PersistedChunk): Promise<void> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(CHUNK_STORE, "readwrite");
    tx.oncomplete = () => resolve();
    tx.onerror = () => fail(reject, tx.error);
    tx.onabort = () => fail(reject, tx.error);
    tx.objectStore(CHUNK_STORE).put(chunk);
  });
}

export function deleteSessionRecords(db: IDBDatabase, sessionId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction([CHUNK_STORE, META_STORE], "readwrite");
    tx.oncomplete = () => resolve();
    tx.onerror = () => fail(reject, tx.error);
    tx.onabort = () => fail(reject, tx.error);
    tx.objectStore(META_STORE).delete(sessionId);
    const request = tx.objectStore(CHUNK_STORE).index("by_session").getAllKeys(sessionId);
    request.onsuccess = () => {
      for (const key of request.result) {
        tx.objectStore(CHUNK_STORE).delete(key);
      }
    };
    request.onerror = () => fail(reject, request.error);
  });
}

export async function purgeExpiredMeta(
  db: IDBDatabase,
  meta: RecordingMeta,
  now: number,
): Promise<boolean> {
  if (!shouldDiscardMeta(meta, now)) {
    return false;
  }
  await deleteSessionRecords(db, meta.session_id);
  return true;
}
