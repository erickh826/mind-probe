// Decides when a tab with no session credential should ask for an upload code.
// IndexedDB keeps the session id and the blobs. It does not keep the token.
// A retained take with nothing left to send is not a candidate.

import { listChunks, listMeta, openRecordingDb } from "./recordingDb.ts";
import {
  showUploadCodeEntry,
  uploadWorkChunkCount,
  type RecordingMeta,
} from "./uploadQueue.ts";

export interface UploadRecovery {
  sessionId: string;
}

export async function findUnfinishedUploadSessions(): Promise<UploadRecovery[]> {
  const database = await openRecordingDb();
  try {
    const metas = await listMeta(database);
    const found: UploadRecovery[] = [];
    for (const meta of metas) {
      if (await metaNeedsUploadCode(database, meta)) {
        found.push({ sessionId: meta.session_id });
      }
    }
    return found;
  } finally {
    database.close();
  }
}

async function metaNeedsUploadCode(
  database: Awaited<ReturnType<typeof openRecordingDb>>,
  meta: RecordingMeta,
): Promise<boolean> {
  const chunks = await listChunks(database, meta.session_id);
  return showUploadCodeEntry(meta, uploadWorkChunkCount(chunks));
}
