// Decides when a tab with no session credential should ask for an upload code.
// IndexedDB keeps the session id and the blobs. It does not keep the token.

import { listChunks, listMeta, openRecordingDb } from "./recordingDb.ts";
import { showUploadCodeEntry, type RecordingMeta } from "./uploadQueue.ts";

export interface UploadRecovery {
  sessionId: string;
}

export async function findUnfinishedUploadSession(): Promise<UploadRecovery | null> {
  const database = await openRecordingDb();
  try {
    const metas = await listMeta(database);
    for (const meta of metas) {
      if (await metaNeedsUploadCode(database, meta)) {
        return { sessionId: meta.session_id };
      }
    }
    return null;
  } finally {
    database.close();
  }
}

async function metaNeedsUploadCode(
  database: Awaited<ReturnType<typeof openRecordingDb>>,
  meta: RecordingMeta,
): Promise<boolean> {
  const chunks = await listChunks(database, meta.session_id);
  return showUploadCodeEntry(meta, chunks.length);
}
